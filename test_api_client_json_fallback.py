"""Regression coverage for Gemma's json_object grammar incompatibility."""
import copy
import unittest

import pact_translate_v3 as runtime


class Response:
    ok = True

    def json(self):
        return {
            "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
        }


class ApiClientJsonFallbackTests(unittest.TestCase):
    def test_gemma_format_error_retries_once_without_response_format(self):
        cfg = copy.deepcopy(runtime.DEFAULTS["translator_api"])
        cfg.update({"http_retries": 1, "timeout_seconds": 1})
        client = runtime.ApiClient(cfg, "translator")
        payloads = []

        def post(_url, json, timeout):
            payloads.append(copy.deepcopy(json))
            if "response_format" in json:
                response = Response()
                response.ok = False
                response.status_code = 500
                response.reason = "Internal Server Error"
                response.text = (
                    '{"error":{"message":"The model produced output that does not '
                    'match the expected peg-gemma4 format"}}'
                )
                return response
            return Response()

        client.session.post = post
        stage = {"temperature": 0.0, "top_p": 1.0, "top_k": 1}
        result = client.complete([{"role": "user", "content": "{}"}], stage, 16, "test")

        self.assertEqual("{}", result.content)
        self.assertIn("response_format", payloads[0])
        self.assertNotIn("response_format", payloads[1])

        client.complete([{"role": "user", "content": "{}"}], stage, 16, "test-2")
        self.assertNotIn("response_format", payloads[2])


if __name__ == "__main__":
    unittest.main()