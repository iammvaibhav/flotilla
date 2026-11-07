import json

import pytest
import yaml

from flotilla import bundles, layers
from flotilla.state import Fleet, Host, load_fleet


def test_override_keys_merge_add_remove_replace_delete():
    base = {"roles": {"default": "a", "plan": "b"},
            "disabled": ["x", "y"],
            "retry": {"chains": {"default": ["p"], "task": ["q"]}},
            "auth": {"broker": {"url": "u"}}}
    over = {"roles.default": "c",
            "disabled+": ["y", "z"],
            "disabled-": ["x"],
            "retry": {"chains!": {"default": ["r"]}},
            "auth.broker": None}
    prov = {}
    layers.leaves(base, "base", prov)
    out = layers.apply(base, over, "group:work", prov)
    assert out == {"roles": {"default": "c", "plan": "b"},
                   "disabled": ["y", "z"],
                   "retry": {"chains": {"default": ["r"]}},
                   "auth": {}}
    assert base["roles"]["default"] == "a"   # input untouched
    by_key = {row["key"]: row["sources"] for row in layers.explain(out, prov)}
    assert by_key["roles.default"] == ["group:work"]
    assert by_key["roles.plan"] == ["base"]
    assert by_key["disabled"] == ["base", "group:work+", "group:work-"]
    assert by_key["retry.chains.default"] == ["group:work"]


def test_dots_split_only_top_level_keys():
    base = {"providers": {"llama.cpp": {"on": True}}}
    out = layers.apply(base, {"providers": {"llama.cpp": {"on": False}}}, "h")
    assert out == {"providers": {"llama.cpp": {"on": False}}}


def test_list_ops_need_lists():
    with pytest.raises(layers.LayerError):
        layers.apply({"m": {"a": 1}}, {"m+": ["b"]}, "h")
    with pytest.raises(layers.LayerError):
        layers.apply({}, {"a!": {"b+": [1]}}, "h")


def test_remove_key_handles_dotted_and_nested_forms():
    doc = {"a.b": 1, "a": {"c": {"d!": 2}, "e": 3}, "f+": [1]}
    assert layers.remove_key(doc, ("a", "c", "d"))
    assert layers.remove_key(doc, ("a", "b"))
    assert layers.remove_key(doc, ("f",))
    assert doc == {"a": {"e": 3}}
    assert not layers.remove_key(doc, ("zz",))


def _bundle(tmp_path, spec, files):
    root = tmp_path / "bundles" / "b"
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    (root / "bundle.yaml").write_text(yaml.safe_dump(spec))
    return bundles.load_bundles(tmp_path)["b"]


def _fleet():
    hosts = {
        "one": Host("one", "10.0.0.1", "u", "linux-x64", ["personal"]),
        "two": Host("two", "10.0.0.2", "u", "linux-x64", ["work"]),
        "out": Host("out", "10.0.0.3", "u", "linux-x64", [], managed=False),
    }
    return Fleet(controller="one", hosts=hosts)


def test_layers_apply_base_then_group_then_host(tmp_path):
    fleet = _fleet()
    b = _bundle(tmp_path, {"dest": "~/x", "files": [
        {"match": "*.yml", "merge": "keys"},
        {"match": "*.json", "merge": "keys", "own_keys": ["a"]}]}, {
        "base/config.yml": "# comment kept\nk: base\nl: [1]\n",
        "groups/work/config.yml": "k: work\nl+: [2]\n",
        "hosts/two/config.yml": "k: two\n",
        "base/notes.md": "base notes\n",
        "groups/work/notes.md": "work notes\n",
        "base/settings.json.yml": "# yaml in, json out\na: {b: 1}\n",
        "base/AGENTS.md.j2": "{% for h in hosts %}{{ h.name }} {% endfor %}on {{ me.name }}\n",
    })
    one = bundles.compile_bundle(b, fleet.host("one"), fleet)
    assert one.files["config.yml"].text == "# comment kept\nk: base\nl: [1]\n"
    assert one.files["notes.md"].text == "base notes\n"
    assert json.loads(one.files["settings.json"].text) == {"a": {"b": 1}}
    assert one.files["settings.json"].own_keys == ["a"]
    assert one.files["AGENTS.md"].text == "one two on one\n"   # managed only
    two = bundles.compile_bundle(b, fleet.host("two"), fleet)
    assert yaml.safe_load(two.files["config.yml"].text) == {"k": "two",
                                                            "l": [1, 2]}
    assert two.files["config.yml"].sources == ["base", "group:work",
                                               "host:two"]
    assert two.files["notes.md"].text == "work notes\n"


def test_bad_override_is_reported_for_the_host(tmp_path):
    fleet = _fleet()
    b = _bundle(tmp_path, {"dest": "~/x", "files": [
        {"match": "*.yml", "merge": "keys"}]}, {
        "base/c.yml": "m: {a: 1}\n", "hosts/two/c.yml": "m+: [b]\n"})
    problems = bundles.check(b, fleet)
    assert len(problems) == 1 and problems[0].startswith("two:")


def test_directory_items_skip_delete_and_replace_whole(tmp_path):
    fleet = _fleet()
    b = _bundle(tmp_path, {"dest": "~/skills", "unit": "directory"}, {
        "base/alpha/SKILL.md": "alpha base\n",
        "base/alpha/extra.md": "only in base\n",
        "groups/personal/alpha/SKILL.md": "alpha personal\n",
        "base/beta/SKILL.md": "beta\n",
        "base/gamma/SKILL.md": "gamma\n",
        "groups/personal/_layer.yml": "skip: [beta]\ndelete: [old]\n",
        "hosts/one/beta/SKILL.md": "beta for one\n",
        "hosts/two/_layer.yml": "skip: [gamma]\n",
    })
    one = bundles.compile_bundle(b, fleet.host("one"), fleet)
    # The personal copy of alpha replaces the whole folder.
    assert sorted(one.files) == ["alpha/SKILL.md", "beta/SKILL.md",
                                 "gamma/SKILL.md"]
    assert one.items == {"alpha": "group:personal", "beta": "host:one",
                         "gamma": "base"}
    assert one.delete == ["old"]
    two = bundles.compile_bundle(b, fleet.host("two"), fleet)
    assert two.skipped == ["gamma"]
    assert "gamma/SKILL.md" not in two.files


def test_set_and_clear_override_keep_the_rest_of_the_file(tmp_path):
    fleet = _fleet()
    b = _bundle(tmp_path, {"dest": "~/x", "files": [
        {"match": "*.yml", "merge": "keys"}]}, {
        "base/c.yml": "a: {b: 1, c: 2}\nl: [x]\n",
        "groups/work/c.yml": "# why work differs\na.b: 5\n"})
    bundles.set_override(b, "group:work", "c.yml", "l", ["y"], "+")
    path = b.layer_dir("group:work") / "c.yml"
    assert path.read_text() == "# why work differs\na.b: 5\nl+:\n  - y\n"
    two = bundles.compile_bundle(b, fleet.host("two"), fleet)
    assert yaml.safe_load(two.files["c.yml"].text) == {
        "a": {"b": 5, "c": 2}, "l": ["x", "y"]}
    # Replacing a key keeps the comment and the other key.
    bundles.set_override(b, "group:work", "c.yml", "a.b", 6)
    assert path.read_text() == "# why work differs\nl+:\n  - y\na.b: 6\n"
    assert bundles.clear_override(b, "group:work", "c.yml", "l")
    assert path.read_text() == "# why work differs\na.b: 6\n"
    assert not bundles.clear_override(b, "group:work", "c.yml", "l")
    assert bundles.clear_override(b, "group:work", "c.yml", "a.b")
    assert not path.exists()


HOSTS_YAML = """\
controller: one
hosts:
  one: {address: 10.0.0.1, user: u, platform: linux-x64, groups: [personal]}
  two: {address: 10.0.0.2, user: u, platform: linux-x64, groups: [work]}
"""


def test_a_change_that_breaks_the_state_is_undone(tmp_path):
    (tmp_path / "hosts.yaml").write_text(HOSTS_YAML)
    b = _bundle(tmp_path, {"dest": "~/x", "files": [
        {"match": "*.yml", "merge": "keys"}]}, {
        "base/c.yml": "m: {a: 1}\n", "groups/work/c.yml": "x: 1\n"})
    over = b.layer_dir("group:work") / "c.yml"
    # `+` on a map does not compile for host two: the old file comes back.
    problems = bundles.guarded_edit(
        over, lambda: over.write_text("m+: [b]\n"), root=tmp_path)
    assert problems and problems[0].startswith("two:")
    assert over.read_text() == "x: 1\n"
    # A new broken file is removed again, with the folders made for it.
    new = b.layer_dir("host:two") / "c.yml"

    def create():
        new.parent.mkdir(parents=True)
        new.write_text("m+: [b]\n")
    assert bundles.guarded_edit(new, create, root=tmp_path)
    assert not (tmp_path / "bundles/b/hosts").exists()
    # A broken hosts.yaml is restored too; a good edit stays.
    hosts = tmp_path / "hosts.yaml"
    assert bundles.guarded_edit(
        hosts, lambda: hosts.write_text("hosts: {one: {}}\n"), root=tmp_path)
    assert hosts.read_text() == HOSTS_YAML
    assert bundles.guarded_edit(
        over, lambda: over.write_text("m.a: 2\n"), root=tmp_path) == []
    assert over.read_text() == "m.a: 2\n"


def test_compare_shows_what_a_layer_changes_from_base(tmp_path):
    (tmp_path / "hosts.yaml").write_text(HOSTS_YAML)
    b = _bundle(tmp_path, {"dest": "~/x", "files": [
        {"match": "*.yml", "merge": "keys"}]}, {
        "base/c.yml": "# base\na: {b: 1, c: 2}\nl: [x]\n",
        "groups/work/c.yml": "a.b: 5\nl+: [y]\n",
        "hosts/two/c.yml": "l-: [x]\n",
        "base/notes.md": "one\ntwo\n",
        "groups/work/notes.md": "one\nthree\n"})
    fleet = load_fleet(tmp_path)

    def changed(diff):
        return [ln for ln in diff.splitlines()
                if ln[:1] in "+-" and ln[:3] not in ("+++", "---")]

    work = bundles.compare(b, "c.yml", "group:work", fleet)
    assert changed(work["diff"]) == ["-  b: 1", "+  b: 5", "+  - y"]
    # Unsaved edits are compared instead of the file.
    draft = bundles.compare(b, "c.yml", "group:work", fleet, "a.c: 3\n")
    assert changed(draft["diff"]) == ["-  c: 2", "+  c: 3"]
    # A host's file is compared as the host gets it, its group included.
    two = bundles.compare(b, "c.yml", "host:two", fleet)
    assert "what two gets (base + work + two)" in two["diff"]
    assert changed(two["diff"]) == ["-  b: 1", "+  b: 5", "-  - x", "+  - y"]
    notes = bundles.compare(b, "notes.md", "group:work", fleet)
    assert changed(notes["diff"]) == ["-two", "+three"]
