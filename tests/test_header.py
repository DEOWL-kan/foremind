import unittest

from foremind.header import HeaderError, parse, render


class HeaderTest(unittest.TestCase):
    def test_round_trip(self):
        h = {"id": "M1.3", "title": "基础：契约批", "owns_paths": ["main:foremind/a.py", "中文/路径"],
             "budget": {"tokens": 12000, "note": "估计"}, "empty": ""}
        body = "# 正文\n\n---\n不是头\n"
        text = render(h, body)
        self.assertTrue(text.startswith("---\nid: M1.3\ntitle: 基础：契约批\n"))
        self.assertIn('owns_paths: ["main:foremind/a.py", "中文/路径"]\n', text)
        self.assertEqual(parse(text), (h, body))
        self.assertEqual(list(parse(text)[0]), list(h))  # key order kept

    def test_no_header(self):
        for text in ("plain\n---\n", "", "--- \nx"):
            self.assertEqual(parse(text), ({}, text))

    def test_value_with_colon_and_crlf(self):
        self.assertEqual(parse("---\r\nurl: http://x:1/a\r\n---\r\nb"), ({"url": "http://x:1/a"}, "b"))

    def test_errors_carry_line_numbers(self):
        cases = {
            "---\na: 1\nno colon here\n---\n": "line 3",
            "---\na: [1, 2\n---\n": "line 2",
            "---\na: 1\na: 2\n---\n": "line 3",
            "---\na: 1\n": "line 1",
        }
        for text, where in cases.items():
            with self.assertRaises(HeaderError) as cm:
                parse(text)
            self.assertIn(where, str(cm.exception), text)

    def test_render_refuses_values_that_would_not_round_trip(self):
        for bad in ({"n": 3}, {"s": " pad"}, {"s": "a\nb"}, {"s": "[not json"}, {"a:b": "x"}, {"": "x"}):
            with self.assertRaises(HeaderError):
                render(bad, "")


if __name__ == "__main__":
    unittest.main()
