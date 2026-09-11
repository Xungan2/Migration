import unittest

from porter.provider import response


class ResponseTest(unittest.TestCase):
    def test_response_with_explanation(self):
        payload = '{"action":"task","nested":{"text":"brace } in string"}}'
        expected = {'action': 'task', 'nested': {'text': 'brace } in string'}}
        for text in (payload, f'```json\n{payload}\n```',
                     f'调查完成。\n{payload}', f'调查完成。\n```json\n{payload}\n```'):
            with self.subTest(text=text):
                self.assertEqual(response(text), expected)
        for text in ('not JSON', '[]', '[{"action":"task"}]',
                     '调查完成。\n[{"action":"task"}]',
                     '调查完成。\n{"broken":\n{"action":"task"}',
                     f'调查完成。\n{payload}\n{payload}',
                     f'调查完成。\n{payload}\ntrailing text'):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    response(text)
