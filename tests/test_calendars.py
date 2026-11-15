import csv
import importlib.util
import json
import re
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


chinese = module("chinese", "proj-chinese-calendar/generate.py")
festivals = module("festivals", "proj-festivals/generate.py")
site = module("site_builder", "scripts/build_site.py")


class CalendarTests(unittest.TestCase):
    def test_screenshot_reference(self):
        item = chinese.day_details(date(2026, 8, 21))
        self.assertEqual(item["lunar_date"], "七月初九")
        self.assertEqual(item["ganzhi"], "丙午年 丙申月 丁卯日")
        self.assertEqual((item["deity"], item["day_type"], item["luck"]), ("朱雀", "黑道日", "凶"))
        self.assertEqual(set(item["yi"]), set("嫁娶 出行 解除 移徙 立券 交易 入宅 开市 祈福 栽种 安床 安葬 祭祀 安香 出火 伐木 开光 求嗣".split()))
        self.assertEqual(set(item["ji"]), set("理发 动土 作灶 掘井 开池 破土".split()))
        description = chinese.description(item)
        self.assertNotIn("来源", description)
        self.assertTrue(all(not line.startswith(" ") for line in description.splitlines()))

    def test_lunar_reference_and_term_boundary(self):
        for day, expected in [(date(2026, 2, 17), "正月初一"), (date(2026, 8, 27), "七月十五"), (date(2025, 7, 25), "闰六月初一")]:
            with self.subTest(day=day):
                self.assertEqual(chinese.day_details(day)["lunar_date"], expected)
        self.assertTrue(chinese.day_details(date(2026, 2, 3))["ganzhi"].startswith("乙巳年 己丑月"))
        self.assertTrue(chinese.day_details(date(2026, 2, 4))["ganzhi"].startswith("丙午年 庚寅月"))

    def test_ganzhi_event_titles(self):
        now = datetime(2026, 11, 15, tzinfo=timezone.utc)
        samples = [
            (date(2026, 11, 15), "癸巳日 · 己亥月 · 丙午年"),
            (date(2026, 8, 21), "丁卯日 · 丙申月 · 丙午年"),
            (date(2026, 2, 3), "戊申日 · 己丑月 · 乙巳年"),
            (date(2026, 2, 4), "己酉日 · 庚寅月 · 丙午年"),
            (date(2025, 7, 25), "乙未日 · 癸未月 · 乙巳年"),
        ]
        for day, expected in samples:
            with self.subTest(day=day):
                item = chinese.day_details(day)
                data = chinese.render_calendar([item], "每日黄历", now)
                unfolded = data.decode("utf-8").replace("\r\n ", "")
                summaries = [line for line in unfolded.split("\r\n") if line.startswith("SUMMARY:")]
                self.assertEqual(summaries, ["SUMMARY:" + expected])
                self.assertIn("DESCRIPTION:" + chinese.escape_text(chinese.description(item)) + "\r\n", unfolded)

    def test_rolling_window_format_and_stable_uid(self):
        now = datetime(2026, 12, 31, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp)
            chinese.generate(date(2026, 12, 31), now, target)
            data = (target / "daily.ics").read_bytes()
            self.assertNotIn(b"\n", data.replace(b"\r\n", b""))
            self.assertTrue(all(len(line) <= 75 for line in data.split(b"\r\n")))
            unfolded = data.decode().replace("\r\n ", "")
            config = json.loads((ROOT / "proj-chinese-calendar/config.json").read_text(encoding="utf-8"))
            past, future = config["past_days"], config["future_days"]
            count = past + future + 1
            self.assertEqual(unfolded.count("BEGIN:VEVENT"), count)
            self.assertNotIn("VALARM", unfolded)
            self.assertNotIn("RRULE", unfolded)
            snapshot = json.loads((target / "snapshot.json").read_text(encoding="utf-8"))
            days = [date.fromisoformat(item["date"]) for item in snapshot["days"]]
            self.assertEqual(days, [date(2026, 12, 31) - timedelta(days=past) + timedelta(days=i) for i in range(count)])
            self.assertEqual(len(set(days)), count)
            self.assertEqual(snapshot["start_date"], days[0].isoformat())
            self.assertEqual(snapshot["end_date"], days[-1].isoformat())
            for event in unfolded.split("BEGIN:VEVENT\r\n")[1:]:
                props = dict(line.split(":", 1) for line in event.split("\r\nEND:VEVENT")[0].split("\r\n"))
                start = datetime.strptime(props["DTSTART;VALUE=DATE"], "%Y%m%d").date()
                self.assertEqual(props["DTEND;VALUE=DATE"], (start + timedelta(days=1)).strftime("%Y%m%d"))
                self.assertEqual(props["UID"], f"chinese-calendar-{start.isoformat()}@festival-calendar")
            first_uids = {line for line in unfolded.splitlines() if line.startswith("UID:")}
            chinese.generate(date(2027, 1, 1), now + timedelta(days=1), target)
            next_uids = {line for line in (target / "daily.ics").read_text(encoding="utf-8").splitlines() if line.startswith("UID:")}
            self.assertEqual(len(first_uids & next_uids), count - 1)

    def test_widget_month_window(self):
        config = json.loads((ROOT / "proj-chinese-calendar/config.json").read_text(encoding="utf-8"))
        self.assertEqual((config["past_days"], config["future_days"]), (45, 70))
        for today in (date(2026, 1, 1), date(2026, 11, 15), date(2026, 12, 31), date(2028, 2, 29)):
            with self.subTest(today=today):
                start = today - timedelta(days=config["past_days"])
                end = today + timedelta(days=config["future_days"])
                month_start = today.replace(day=1)
                next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
                month_end = next_month - timedelta(days=1)
                self.assertLessEqual(start, month_start)
                self.assertGreaterEqual(end, month_end)

    def test_failure_preserves_previous_subscription(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp)
            (target / "daily.ics").write_bytes(b"last successful file")
            with patch.object(chinese, "day_details", side_effect=ValueError("bad data")):
                with self.assertRaises(ValueError):
                    chinese.generate(date(2026, 9, 20), datetime.now(timezone.utc), target)
            self.assertEqual((target / "daily.ics").read_bytes(), b"last successful file")

    def test_festival_generation_preserves_published_bytes(self):
        originals = {
            "festival_extra.ics": festivals.OUTPUT_PATH.read_bytes(),
            "festival_zh.ics": festivals.ZH_OUTPUT_PATH.read_bytes(),
        }
        with tempfile.TemporaryDirectory() as temp:
            outputs = {name: Path(temp) / name for name in originals}
            for name, output in outputs.items():
                output.write_bytes(originals[name])
            with (
                patch.object(festivals, "OUTPUT_PATH", outputs["festival_extra.ics"]),
                patch.object(festivals, "ZH_OUTPUT_PATH", outputs["festival_zh.ics"]),
            ):
                festivals.main()
            for name, output in outputs.items():
                self.assertEqual(output.read_bytes(), originals[name])

    def test_pure_chinese_festival_calendar(self):
        bilingual = festivals.OUTPUT_PATH.read_text(encoding="utf-8")
        chinese_only = festivals.ZH_OUTPUT_PATH.read_text(encoding="utf-8")
        self.assertEqual(
            bilingual.count("BEGIN:VEVENT"),
            chinese_only.count("BEGIN:VEVENT"),
        )
        self.assertIn("X-WR-CALNAME:节日补充日历（纯中文）", chinese_only)

        unfolded = chinese_only.replace("\n ", "")
        for line in unfolded.splitlines():
            if line.startswith(("SUMMARY:", "DESCRIPTION:")):
                self.assertNotRegex(line.split(":", 1)[1], r"[A-Za-z]")

        chinese_uids = re.findall(r"^UID:(.*)$", unfolded, re.MULTILINE)
        self.assertTrue(chinese_uids)
        self.assertTrue(all(uid.endswith("@festival-calendar-zh") for uid in chinese_uids))

    def test_festival_scope_for_traditional_days_and_war_victory(self):
        with (ROOT / "proj-festivals/festivals.csv").open(encoding="utf-8-sig", newline="") as file:
            rows = list(csv.DictReader(file))
        by_id = {row["id"]: row for row in rows}

        victory = by_id["war-victory-day"]
        self.assertEqual(
            (victory["name"], victory["date"], victory["repeat"], victory["category"]),
            (
                "中国人民抗日战争胜利纪念日 Victory Day of the Chinese People's War of Resistance Against Japanese Aggression",
                "2026/9/3",
                "yearly",
                "commemoration",
            ),
        )
        self.assertEqual(victory["enabled"], "TRUE")

        for prefix in ("xiaonian-north-lunar", "xiaonian-south-lunar"):
            entries = [row for row in rows if row["id"].startswith(prefix)]
            self.assertEqual(len(entries), 275)
            self.assertTrue(all(row["enabled"] == "TRUE" for row in entries))
            self.assertTrue(all(row["category"] == "lunar" for row in entries))

        laba_entries = [row for row in rows if row["id"].startswith("laba-lunar")]
        self.assertEqual(len(laba_entries), 275)
        self.assertTrue(all(row["enabled"] == "TRUE" for row in laba_entries))
        self.assertTrue(all(row["category"] == "lunar" for row in laba_entries))

        self.assertFalse(
            any(
                row["enabled"] == "TRUE"
                and row["id"].startswith(("spring-festival-lunar", "chuxi-lunar"))
                for row in rows
            )
        )

    def test_site_keeps_old_subscription_path(self):
        with tempfile.TemporaryDirectory() as temp:
            site.build_site(Path(temp))
            self.assertEqual((Path(temp) / "festival_extra.ics").read_bytes(), festivals.OUTPUT_PATH.read_bytes())
            self.assertEqual((Path(temp) / "festival_zh.ics").read_bytes(), festivals.ZH_OUTPUT_PATH.read_bytes())
            self.assertTrue((Path(temp) / "chinese-calendar/daily.ics").is_file())
            widget_json = Path(temp) / "chinese-calendar/snapshot.json"
            self.assertEqual(widget_json.read_bytes(), (ROOT / "proj-chinese-calendar/snapshot.json").read_bytes())
            published = json.loads(widget_json.read_text(encoding="utf-8"))
            self.assertTrue(published["days"])
            required = {"date", "lunar_date", "ganzhi", "yi", "ji", "deity", "day_type", "luck"}
            self.assertTrue(all(required.issubset(day) for day in published["days"]))
            self.assertTrue((Path(temp) / "festivals/index.html").is_file())


if __name__ == "__main__":
    unittest.main()
