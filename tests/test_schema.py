from __future__ import annotations

import json
import unittest
from pathlib import Path


class WorkerSchemaTests(unittest.TestCase):
    def test_worker_result_schema_matches_codex_structured_output_constraints(self) -> None:
        schema_path = (
            Path(__file__).resolve().parents[1]
            / "codex_automate"
            / "schemas"
            / "worker_result.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        def walk(node: object, path: tuple[str, ...] = ()) -> None:
            if isinstance(node, dict):
                properties = node.get("properties")
                if isinstance(properties, dict):
                    self.assertFalse(
                        node.get("additionalProperties", True),
                        f"{'.'.join(path) or '<root>'} must set additionalProperties to false",
                    )
                    self.assertEqual(
                        set(node.get("required", [])),
                        set(properties.keys()),
                        f"{'.'.join(path) or '<root>'} must require every declared property",
                    )
                for key, value in node.items():
                    walk(value, path + (key,))
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, path + (str(index),))

        walk(schema)


if __name__ == "__main__":
    unittest.main()
