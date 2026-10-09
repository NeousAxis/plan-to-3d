"""Tests hors réseau du routeur de rendu (catalogue, validation, coût, budget).

  python3 -m unittest tests/test_render_router.py
"""
import copy
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import render_router as rr  # noqa: E402
from PIL import Image  # noqa: E402


def png(w, h):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (120, 110, 100)).save(buf, format="PNG")
    return buf.getvalue()


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.cat = rr.load_catalog()

    def test_catalog_has_no_google_and_only_free_models(self):
        for m in self.cat["models"]:
            self.assertNotIn(m["creator"], self.cat["deny"]["creators"], m["id"])
            self.assertTrue(m["free"], m["id"])

    def test_denied_creator_is_refused(self):
        cat = copy.deepcopy(self.cat)
        fake = copy.deepcopy(cat["models"][0])
        fake.update(id="nano-banana", creator="Google")
        cat["models"].append(fake)
        with self.assertRaises(rr.RouterError) as ctx:
            rr.build(rr.RenderRequest("nano-banana", "x", [png(64, 64)]), cat)
        self.assertEqual(ctx.exception.code, "forbidden_model")

    def test_unknown_model(self):
        with self.assertRaises(rr.RouterError) as ctx:
            rr.build(rr.RenderRequest("nope", "x"), self.cat)
        self.assertEqual(ctx.exception.code, "unsupported_model")


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.cat = rr.load_catalog()

    def test_text_only_model_refuses_reference(self):
        with self.assertRaises(rr.RouterError) as ctx:
            rr.build(rr.RenderRequest("flux-1-schnell", "x", [png(64, 64)]), self.cat)
        self.assertEqual(ctx.exception.code, "invalid_input")

    def test_edit_only_model_needs_reference(self):
        with self.assertRaises(rr.RouterError) as ctx:
            rr.build(rr.RenderRequest("flux-1-kontext-dev", "x", [], provider="hf-space"), self.cat)
        self.assertIn(ctx.exception.code, ("invalid_input", "unsupported_provider"))

    def test_too_many_references(self):
        with self.assertRaises(rr.RouterError) as ctx:
            rr.build(rr.RenderRequest("flux-2-klein-4b", "x", [png(64, 64)] * 5), self.cat)
        self.assertEqual(ctx.exception.code, "invalid_input")

    def test_empty_prompt(self):
        with self.assertRaises(rr.RouterError):
            rr.build(rr.RenderRequest("flux-2-klein-4b", "   ", [png(64, 64)]), self.cat)

    def test_reference_is_shrunk_under_cloudflare_limit(self):
        plan = rr.build(rr.RenderRequest("flux-2-klein-4b", "x", [png(1280, 720)], provider="cloudflare"), self.cat)
        self.assertLessEqual(max(plan.ref_sizes[0]), 511)
        w, h = Image.open(io.BytesIO(plan.refs[0])).size
        self.assertLessEqual(max(w, h), 511)
        self.assertEqual((plan.width, plan.height), (1280, 720))

    def test_hf_space_caps_output_side(self):
        if not rr.hf_available():
            self.skipTest("gradio_client absent")
        plan = rr.build(rr.RenderRequest("flux-2-klein-4b", "x", [png(1280, 720)], provider="hf-space"), self.cat)
        self.assertEqual((plan.width, plan.height), (1024, 576))


class EstimateTests(unittest.TestCase):
    def setUp(self):
        self.cat = rr.load_catalog()

    def est(self, model, refs=1, aspect="16:9"):
        plan = rr.build(rr.RenderRequest(model, "x", [png(1280, 720)] * refs, aspect, provider="cloudflare"), self.cat)
        return rr.estimate(plan)

    def test_klein_4b_cost(self):
        # 1 tuile d'entrée (511×287) + 3×2 tuiles de sortie (1280×720)
        self.assertAlmostEqual(self.est("flux-2-klein-4b")["amount"], round(5.37 + 6 * 26.05, 1))

    def test_klein_9b_cost(self):
        # 1280×720 = 0,88 MP → 1 MP facturé ; entrée 511×287 → 1 MP
        self.assertAlmostEqual(self.est("flux-2-klein-9b")["amount"], round(1363.64 + 181.82, 1))

    def test_dev_cost_scales_with_steps(self):
        self.assertAlmostEqual(self.est("flux-2-dev")["amount"], round(20 * (18.75 + 6 * 37.5), 1))

    def test_schnell_text_only_cost(self):
        self.assertAlmostEqual(self.est("flux-1-schnell", refs=0)["amount"], round(6 * 4.8 + 8 * 9.6, 1))


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        self.tmp.close()
        os.remove(self.tmp.name)
        self.ledger = rr.Ledger(1000, self.tmp.name)

    def tearDown(self):
        if os.path.exists(self.tmp.name):
            os.remove(self.tmp.name)

    def test_reserve_then_block_then_refund(self):
        self.assertAlmostEqual(self.ledger.reserve(600), 400)
        with self.assertRaises(rr.RouterError) as ctx:
            self.ledger.reserve(500)
        self.assertEqual(ctx.exception.code, "quota_exceeded")
        self.ledger.refund(600)
        self.assertEqual(self.ledger.status()["used"], 0)

    def test_new_day_resets_budget(self):
        with open(self.tmp.name, "w") as f:
            json.dump({"day": "2000-01-01", "neurons": 999, "calls": 9}, f)
        self.assertEqual(self.ledger.status()["used"], 0)


if __name__ == "__main__":
    unittest.main()
