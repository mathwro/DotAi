from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotai_app import manifest as manifests, runtime


def skill_manifest(environment: object) -> dict:
    return {
        "version": 1,
        "packages": [],
        "skills": [{"source": "example/skills", "ompEnvironment": environment}],
        "marketplaces": [],
        "plugins": [],
        "mcp": {"target": "mcp.json", "servers": {}},
    }


INVALID_ENVIRONMENTS = (
    None, [], "NAME=value", 1, True,
    {"": "1"}, {"1NAME": "1"}, {"BAD-NAME": "1"}, {"BAD.NAME": "1"},
    {"BAD NAME": "1"}, {"NAME=OTHER": "1"}, {"éNAME": "1"},
    {"NAME\r": "1"}, {"NAME\n": "1"}, {"NAME\0": "1"}, {1: "1"},
    {"NAME": ""}, {"NAME": "two words"}, {"NAME": "value\t"},
    {"NAME": "value\r"}, {"NAME": "value\n"}, {"NAME": "value\0"},
    {"NAME": "$OTHER"}, {"NAME": "${OTHER}"}, {"NAME": "'value'"},
    {"NAME": '"value"'}, {"NAME": "value#comment"}, {"NAME": "value=other"},
    {"NAME": "value;command"}, {"NAME": "value\\path"}, {"NAME": "é"},
    {"NAME": None}, {"NAME": 1}, {"NAME": True}, {"NAME": []}, {"NAME": {}},
)


class SkillEnvironmentValidationTests(unittest.TestCase):
    def test_accepts_literal_environment_and_empty_mapping(self) -> None:
        for environment in ({}, {"_NAME_2": "Az09_./:-", "DISABLE_TELEMETRY": "1"}):
            with self.subTest(environment=environment):
                candidate = skill_manifest(environment)
                validated = manifests.validate_manifest(candidate)
                self.assertEqual(validated["skills"][0]["ompEnvironment"], environment)

    def test_environment_is_optional(self) -> None:
        candidate = skill_manifest({})
        del candidate["skills"][0]["ompEnvironment"]
        self.assertNotIn("ompEnvironment", manifests.validate_manifest(candidate)["skills"][0])

    def test_rejects_nonliteral_names_values_and_nonobjects(self) -> None:
        for environment in INVALID_ENVIRONMENTS:
            with self.subTest(environment=environment):
                with self.assertRaisesRegex(runtime.DotAiError, r"skills\[0\]\.ompEnvironment"):
                    manifests.validate_manifest(skill_manifest(environment))

    def test_invalid_candidate_never_changes_manifest_or_creates_files(self) -> None:
        for environment in INVALID_ENVIRONMENTS:
            with self.subTest(environment=environment), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "stack.json"
                original = (json.dumps(skill_manifest({}), indent=4) + "\n").encode("utf-8")
                path.write_bytes(original)
                candidate = skill_manifest(environment)
                with self.assertRaises(runtime.DotAiError):
                    manifests.write_manifest(path, candidate, backup=True)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(set(root.iterdir()), {path})
                missing = root / "missing" / "stack.json"
                with self.assertRaises(runtime.DotAiError):
                    manifests.write_manifest(missing, candidate)
                self.assertFalse(missing.parent.exists())
                self.assertEqual(set(root.iterdir()), {path})

    def test_valid_literal_environment_survives_manifest_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stack.json"
            manifests.write_manifest(path, skill_manifest({"DISABLE_TELEMETRY": "1"}))
            self.assertEqual(
                manifests.load_manifest(path)["skills"][0]["ompEnvironment"],
                {"DISABLE_TELEMETRY": "1"},
            )


if __name__ == "__main__":
    unittest.main()
