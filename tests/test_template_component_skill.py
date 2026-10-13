"""Guards for the building-apex-template-components skill: the files it names exist and its templates agree with each other."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".agents" / "skills" / "building-apex-template-components"
MIRROR = ROOT / ".claude" / "skills" / "building-apex-template-components" / "SKILL.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TemplateComponentSkillTests(unittest.TestCase):
    def test_description_states_only_when_to_use_it(self) -> None:
        text = read(SKILL / "SKILL.md")
        match = re.search(r"(?m)^description:\s*(.+)$", text)
        self.assertIsNotNone(match)
        self.assertTrue(match.group(1).startswith("Use when"), "the description must state when to use the skill")

    def test_every_file_the_skill_names_exists(self) -> None:
        text = read(SKILL / "SKILL.md")
        named = set(re.findall(r"`((?:reference|templates)/[A-Za-z0-9_.-]+)`", text))
        self.assertGreaterEqual(len(named), 5)
        for relative in sorted(named):
            with self.subTest(file=relative):
                self.assertTrue((SKILL / relative).is_file(), f"{relative} is named in SKILL.md but missing")
        for name in ("plugin.apx", "custom-attributes.apx", "region.apx", "ticket-card.css", "ticket-card.js", "ticket-card-alpine.js"):
            with self.subTest(template=name):
                self.assertIn(name, text, f"{name} is not listed in SKILL.md")
                self.assertTrue((SKILL / "templates" / name).is_file(), f"templates/{name} is missing")

    def test_claude_entry_points_at_the_canonical_skill(self) -> None:
        text = read(MIRROR)
        self.assertIn(".agents/skills/building-apex-template-components/SKILL.md", text)
        self.assertRegex(text, r"(?m)^name: building-apex-template-components$")

    def test_placeholders_attributes_and_region_settings_agree(self) -> None:
        plugin = read(SKILL / "templates" / "plugin.apx")
        attributes = read(SKILL / "templates" / "custom-attributes.apx")
        region = read(SKILL / "templates" / "region.apx")

        declared = re.findall(r"customAttribute ([A-Z0-9_]+) \(\n    apexlangName: (\w+)\n    attribute: (\d+)", attributes)
        self.assertGreaterEqual(len(declared), 3)
        numbers = [int(number) for _, _, number in declared]
        self.assertEqual(len(numbers), len(set(numbers)), "attribute numbers must be unique")
        self.assertTrue(all(1 <= number <= 25 for number in numbers), "a component has at most 25 custom attributes")

        partial = plugin[plugin.index("partial:"):plugin.index("reportBody:")]
        placeholders = {name for name in re.findall(r"#([A-Z][A-Z0-9_]*)#", partial) if not name.startswith("APEX$")}
        self.assertEqual(placeholders, {name for name, _, _ in declared}, "every placeholder needs a customAttribute and vice versa")

        settings = region[region.index("settings {"):]
        settings = settings[:settings.index("}")]
        mapped = dict(re.findall(r"(\w+): ([A-Z0-9_]+)", settings))
        self.assertEqual(set(mapped), {apex_name for _, apex_name, _ in declared}, "every attribute must be mapped in the region settings")
        for column in mapped.values():
            self.assertRegex(region, rf"column {column} \(", f"column {column} needs a column block")
            self.assertRegex(region, rf"\b{column}\b", column)

    def test_region_uses_the_application_component_syntax_and_plain_data(self) -> None:
        region = read(SKILL / "templates" / "region.apx")
        self.assertIn("type: appTemplateComponent/ticketCard", region)
        self.assertIn("display: report", region)
        sql = region[region.index("```sql"):region.index("```", region.index("```sql") + 6)]
        self.assertNotRegex(sql, r"'<[a-zA-Z/]", "the SQL must return data, not HTML")

    def test_alpine_is_optional_and_the_vanilla_template_does_not_need_it(self) -> None:
        text = read(SKILL / "SKILL.md")
        self.assertRegex(text, r"Alpine\.js is \*\*optional\*\*")
        self.assertNotIn("Alpine", read(SKILL / "templates" / "ticket-card.js"))
        alpine = read(SKILL / "templates" / "ticket-card-alpine.js")
        self.assertIn("alpine:init", alpine)
        self.assertIn("BEFORE Alpine", alpine)

    def test_templates_carry_no_credentials_or_outbound_calls(self) -> None:
        for path in sorted((SKILL / "templates").iterdir()):
            with self.subTest(file=path.name):
                body = read(path)
                self.assertNotRegex(body, r"(?i)api[_-]?key|password|bearer |x-session-token|apex_web_service|utl_http")
                self.assertNotRegex(body, r"https?://(?!cdn\.jsdelivr\.net/npm/alpinejs@)")


if __name__ == "__main__":
    unittest.main()
