import tempfile
import unittest

import job_monitor as radar


class JobMonitorTests(unittest.TestCase):
    def test_parses_supported_career_urls(self):
        workday = radar.parse_careers_url(
            "https://cat.wd5.myworkdayjobs.com/en-US/CaterpillarCareers")
        greenhouse = radar.parse_careers_url("https://boards.greenhouse.io/acme")
        oracle = radar.parse_careers_url(
            "https://acme.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_123")

        self.assertEqual(workday["ats"], "workday")
        self.assertEqual(workday["site"], "CaterpillarCareers")
        self.assertEqual(greenhouse, {
            "ats": "greenhouse", "board": "acme", "label": "acme"})
        self.assertEqual(oracle["ats"], "oracle")

    def test_title_exclusions_win_over_inclusions(self):
        filters = {
            "title_keywords": ["data"],
            "exclude_keywords": ["intern"],
        }
        self.assertTrue(radar.title_ok({"title": "Data Engineer"}, filters))
        self.assertFalse(radar.title_ok({"title": "Data Intern"}, filters))

    def test_repost_upsert_refreshes_the_live_link(self):
        with tempfile.NamedTemporaryFile() as db:
            conn = radar.open_db(db.name)
            original = {
                "title": "Data Engineer", "location": "Bengaluru",
                "url": "https://example.com/old", "posted": "2026-08-01",
                "age_days": 10,
            }
            replacement = {
                **original, "url": "https://example.com/new", "age_days": 1,
            }

            self.assertTrue(radar.upsert(conn, "Acme::data engineer::bengaluru",
                                         "Acme", "workday", original, "first"))
            self.assertFalse(radar.upsert(conn, "Acme::data engineer::bengaluru",
                                          "Acme", "workday", replacement, "second"))
            row = conn.execute("SELECT url, age_days, last_seen FROM jobs").fetchone()
            self.assertEqual(dict(row), {
                "url": "https://example.com/new", "age_days": 1,
                "last_seen": "second",
            })
            conn.close()



if __name__ == "__main__":
    unittest.main()
