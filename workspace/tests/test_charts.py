"""The figure generator: valid SVG, the numbers drawn are the numbers recorded, nothing overflows."""

import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))

import charts                                        # noqa: E402

NS = "{http://www.w3.org/2000/svg}"


def texts(svg: str):
    return [t.text for t in ET.fromstring(svg).iter(NS + "text") if t.text]


def row(suite, strategy, lost, **kw):
    return dict(suite=suite, strategy=strategy, lost_updates=lost, **kw)


class ChartTests(unittest.TestCase):
    def test_bar_chart_draws_every_value_and_label(self):
        svg = charts.bar_chart("T", "sub", ["g1", "g2"], {"a": [1.0, 2.0], "b": [0.0, 4.0]}, "y")
        t = texts(svg)
        for expected in ("T", "sub", "g1", "g2", "a", "b", "1.0", "2.0", "0.0", "4.0", "y"):
            self.assertIn(expected, t)
        self.assertEqual(len(list(ET.fromstring(svg).iter(NS + "rect"))), 1 + 2 + 4)      # background, legend, bars

    def test_bar_height_is_proportional_to_the_value(self):
        svg = charts.bar_chart("T", "", ["g"], {"a": [2.0], "b": [4.0]}, "y", ymax=4)
        heights = {r.find(NS + "title").text.split(" / ")[0]: float(r.get("height"))
                   for r in ET.fromstring(svg).iter(NS + "rect") if r.find(NS + "title") is not None}
        self.assertAlmostEqual(heights["b"], 2 * heights["a"], places=1)

    def test_line_chart_places_points_in_order(self):
        svg = charts.line_chart("T", "", [1, 2, 4], {"s": [1, 2, 3]}, "x", "y", ymax=4)
        pts = [c for c in ET.fromstring(svg).iter(NS + "circle")]
        xs = [float(c.get("cx")) for c in pts]
        ys = [float(c.get("cy")) for c in pts]
        self.assertEqual(xs, sorted(xs))
        self.assertEqual(ys, sorted(ys, reverse=True))                                    # larger values are higher up

    def test_dashed_series_has_no_markers(self):
        svg = charts.line_chart("T", "", [1, 2], {"ideal": [1, 2], "real": [1, 1.5]}, "x", "y", dashed=("ideal",))
        self.assertIn("stroke-dasharray", svg)
        self.assertEqual(len(list(ET.fromstring(svg).iter(NS + "circle"))), 2)

    def test_text_is_escaped(self):
        svg = charts.bar_chart("a < b & c", "<x>", ["g"], {"s&t": [1.0]}, "y")
        ET.fromstring(svg)                                                                # still well formed
        self.assertIn("a < b & c", texts(svg))

    def test_lost_updates_figure_averages_per_suite_and_strategy(self):
        rows = [row("counter", "naive", 3), row("counter", "naive", 4), row("counter", "forkmerge", 0),
                row("counter", "serial", 0), row("counter", "forkmerge-honest", 0)]
        t = texts(charts.fig_lost_updates(rows))
        self.assertIn("3.5", t)
        self.assertIn("shared folder", t)
        self.assertIn("counter", t)

    def test_levels_figure_counts_serializable_runs_per_level_and_suite(self):
        rows = [row("counter", "gitlike", 3, serializable=False), row("counter", "gitlike", 0, serializable=True),
                row("counter", "forkmerge", 0, serializable=True), row("counter", "naive", 3, serializable=False),
                row("counter", "serializable", 0, serializable=True), row("counter", "gitlike-checks", 0, serializable=True),
                row("shared", "gitlike", 0, serializable=True), row("shared", "naive", 0, serializable=None)]
        svg = charts.fig_levels(rows)
        t = texts(svg)
        self.assertIn("50%", t)                       # gitlike on counter: one of two runs
        self.assertIn("100%", t)
        self.assertIn("0%", t)
        self.assertIn("git-like", t)
        ET.fromstring(svg)

    def test_scatter_places_points_by_value_and_labels_them(self):
        svg = charts.scatter_chart("T", "s", [("low", 1.0, 0.0), ("high", 4.0, 3.0)], "cost", "loss", ymax=4)
        root = ET.fromstring(svg)
        pts = {c.find(NS + "title").text.split(":")[0]: (float(c.get("cx")), float(c.get("cy"))) for c in root.iter(NS + "circle")}
        self.assertLess(pts["low"][0], pts["high"][0])                          # cheaper is further left
        self.assertGreater(pts["low"][1], pts["high"][1])                       # fewer losses is lower
        self.assertIn("low", texts(svg))
        self.assertIn("high", texts(svg))

    def test_scatter_with_no_points_and_with_markup_in_labels(self):
        ET.fromstring(charts.scatter_chart("T", "s", [], "x", "y"))
        svg = charts.scatter_chart("a<b", "s", [("x & <y>", 1, 1)], "x", "y")
        ET.fromstring(svg)
        self.assertIn("x & <y>", texts(svg))

    def test_cost_loss_figures_use_the_counter_suite_only(self):
        lv = [dict(suite="counter", strategy="gitlike", seconds=0.6, lost_updates=2.8),
              dict(suite="counter", strategy="forkmerge", seconds=1.5, lost_updates=0.0),
              dict(suite="shared", strategy="gitlike", seconds=9.0, lost_updates=9.0)]
        ld = [dict(suite="counter", tag="reapply-oracle", seconds=0.5, lost_updates=0.0)]
        t = texts(charts.fig_cost_loss(lv, ld))
        self.assertIn("git-like", t)
        self.assertIn("+ ladder layer 2", t)
        self.assertNotIn("9", [x for x in t if x == "9"])
        eco = [dict(suite="counter", strategy="gitlike", tokens=18000, lost_updates=3.0)]
        lad = [dict(suite="counter", tag="eco-redo", tokens=57000, lost_updates=0.0),
               dict(suite="counter", tag="eco-layer2", tokens=20000, lost_updates=0.0)]
        t = texts(charts.fig_cost_loss_eco(eco, lad))
        self.assertIn("+ ladder layer 2", t)

    def test_scale_figure_draws_both_queues_and_the_ideal(self):
        rows = [dict(mode=m, tasks=48, validate=0.03, agents=n, seconds=sec) for n, p, b in ((1, 7.0, 9.0), (8, 2.0, 1.3), (32, 2.0, 0.5))
                for m, sec in (("plain", p), ("batched", b))]
        svg = charts.fig_scale_batch(rows)
        t = texts(svg)
        self.assertIn("batched queue", t)
        self.assertIn("ideal", t)
        self.assertTrue(any("1 validation per landing" in x for x in t))
        ET.fromstring(svg)

    def test_levels_figure_with_no_rows_is_still_valid(self):
        ET.fromstring(charts.fig_levels([]))

    def test_every_figure_can_be_made_from_the_recorded_runs_and_fits_its_canvas(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = charts.main(Path(tmp))
            names = sorted(p.name for p in paths)
            optional = ("levels.svg", "cost-loss.svg", "cost-loss-eco.svg", "scale-batch.svg")
            self.assertEqual([n for n in names if n not in optional], ["lost-updates.svg", "mixed-agents.svg", "redo-budget.svg", "scaling.svg"])
            self.assertEqual("levels.svg" in names, bool(charts.load("oracle_levels.jsonl")))
            for p in paths:
                root = ET.fromstring(p.read_text())
                self.assertEqual(root.get("viewBox"), "0 0 760 380")
                for t in root.iter(NS + "text"):
                    if t.get("text-anchor") is None and t.get("y") in ("32", "52"):                # title and subtitle
                        size = float(t.get("font-size"))
                        self.assertLess(24 + len(t.text) * size * 0.58, 760, (p.name, t.text))     # does not run off the right edge

    def test_the_recorded_headline_numbers_are_in_the_figures(self):
        rows = charts.load("oracle_main.jsonl")
        if not rows:
            self.skipTest("no recorded runs")
        naive = [r["lost_updates"] for r in rows if r["suite"] == "counter" and r["strategy"] == "naive"]
        t = texts(charts.fig_lost_updates(rows))
        self.assertIn(f"{sum(naive) / len(naive):.1f}", t)

    def test_scaling_is_computed_against_one_agent(self):
        rows = [dict(strategy=s, parallel=p, seconds=t) for s, p, t in
                [("naive", 1, 4.0), ("naive", 2, 2.0), ("forkmerge", 1, 4.4), ("forkmerge", 2, 2.2)]]
        svg = charts.fig_scaling(rows)
        t = texts(svg)
        self.assertEqual(t.count("1.00x") + t.count("2.00x"), 0)                           # values are in tooltips, not labels
        titles = [e.text for e in ET.fromstring(svg).iter(NS + "title")]
        self.assertIn("fork + merge queue at 2: 2.00x", titles)

    def test_json_rows_load_from_the_results_folder(self):
        self.assertTrue(charts.RESULTS.is_dir())
        self.assertEqual(charts.load("does-not-exist.jsonl"), [])
        json.dumps(charts.load("oracle_scale.jsonl")[:1])


if __name__ == "__main__":
    unittest.main()
