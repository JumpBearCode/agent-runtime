"""SkillLoader: loads SKILL.md files with YAML-like frontmatter."""

from agent_runtime.core.skills import SkillLoader


def test_empty_dir(tmp_path):
    loader = SkillLoader(tmp_path)
    assert loader.skills == {}
    assert loader.get_descriptions() == "(no skills available)"


def test_nonexistent_dir(tmp_path):
    loader = SkillLoader(tmp_path / "does-not-exist")
    assert loader.skills == {}


def test_loads_skill_with_frontmatter(tmp_path):
    skill_dir = tmp_path / "alpha"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: alpha\n"
        "description: First skill\n"
        "tags: foo, bar\n"
        "---\n"
        "Body text here.\n"
    )
    loader = SkillLoader(tmp_path)
    assert "alpha" in loader.skills
    assert loader.skills["alpha"]["meta"]["description"] == "First skill"
    assert loader.skills["alpha"]["body"] == "Body text here."


def test_falls_back_to_dir_name_if_no_name_meta(tmp_path):
    sd = tmp_path / "bravo"
    sd.mkdir()
    (sd / "SKILL.md").write_text("---\ndescription: ok\n---\nbody")
    loader = SkillLoader(tmp_path)
    assert "bravo" in loader.skills


def test_get_content_wraps_with_skill_tag(tmp_path):
    sd = tmp_path / "x"
    sd.mkdir()
    (sd / "SKILL.md").write_text("---\nname: x\ndescription: d\n---\nthe body")
    loader = SkillLoader(tmp_path)
    content = loader.get_content("x")
    assert content.startswith('<skill name="x">')
    assert "the body" in content
    assert content.endswith("</skill>")


def test_get_content_unknown_skill(tmp_path):
    loader = SkillLoader(tmp_path)
    out = loader.get_content("nope")
    assert out.startswith("Error: Unknown skill")


def test_descriptions_list_all(tmp_path):
    for n, desc in [("a", "AA"), ("b", "BB")]:
        sd = tmp_path / n
        sd.mkdir()
        (sd / "SKILL.md").write_text(f"---\nname: {n}\ndescription: {desc}\n---\nbody")
    loader = SkillLoader(tmp_path)
    desc = loader.get_descriptions()
    assert "AA" in desc
    assert "BB" in desc
