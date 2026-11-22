"""Line-level three-way merge (diff3) for text files, and its use in branch merges."""
import pytest

from agentcloud_server.merge3 import merge3
from test_v05_agents import Modern

BASE = "a\nb\nc\nd\ne\nf\ng\n"


def test_non_overlapping_edits_merge_cleanly():
    ours = BASE.replace("b\n", "B-ours\n")
    theirs = BASE.replace("f\n", "F-theirs\n")
    r = merge3(BASE, ours, theirs)
    assert r.clean and r.text == "a\nB-ours\nc\nd\ne\nF-theirs\ng\n"


def test_one_side_changes_take_that_side():
    ours = BASE.replace("c\n", "C\n")
    assert merge3(BASE, ours, BASE).text == ours
    assert merge3(BASE, BASE, ours).text == ours


def test_identical_changes_on_both_sides_are_not_conflicts():
    both = BASE.replace("d\n", "D\n")
    r = merge3(BASE, both, both)
    assert r.clean and r.text == both


def test_overlapping_different_edits_conflict_with_markers():
    ours = BASE.replace("d\n", "D-ours\n")
    theirs = BASE.replace("d\n", "D-theirs\n")
    r = merge3(BASE, ours, theirs, labels=("ours", "theirs"))
    assert not r.clean and len(r.conflicts) == 1
    assert r.text == "a\nb\nc\n<<<<<<< ours\nD-ours\n=======\nD-theirs\n>>>>>>> theirs\ne\nf\ng\n"
    c = r.conflicts[0]
    assert c["ours"] == "D-ours\n" and c["theirs"] == "D-theirs\n" and c["base"] == "d\n" and c["line"] == 4


def test_insertions_at_different_places():
    ours = "x\n" + BASE
    theirs = BASE + "y\n"
    r = merge3(BASE, ours, theirs)
    assert r.clean and r.text == "x\n" + BASE + "y\n"


def test_insertions_at_the_same_place_conflict_unless_identical():
    ours = BASE.replace("c\n", "c\nours-insert\n")
    theirs = BASE.replace("c\n", "c\ntheirs-insert\n")
    assert not merge3(BASE, ours, theirs).clean
    assert merge3(BASE, ours, ours).text == ours


def test_deletions():
    ours = BASE.replace("b\n", "")
    theirs = BASE.replace("f\n", "")
    r = merge3(BASE, ours, theirs)
    assert r.clean and r.text == "a\nc\nd\ne\ng\n"
    # delete vs edit of the same line is a conflict
    assert not merge3(BASE, BASE.replace("b\n", ""), BASE.replace("b\n", "B\n")).clean


def test_adjacent_edits_conflict():
    """Changes to neighbouring lines touch the same hunk; diff3 treats them as a conflict."""
    ours = BASE.replace("c\n", "C\n")
    theirs = BASE.replace("d\n", "D\n")
    r = merge3(BASE, ours, theirs)
    assert not r.clean


def test_empty_base_and_missing_trailing_newline():
    assert merge3("", "same\n", "same\n").text == "same\n"
    assert not merge3("", "one\n", "two\n").clean
    r = merge3("a\nb", "A\nb", "a\nB")            # no trailing newline anywhere
    assert not r.clean or r.text == "A\nB"
    r = merge3("a\nb\nc\nd", "A\nb\nc\nd", "a\nb\nc\nD")
    assert r.clean and r.text == "A\nb\nc\nD"


def test_many_hunks():
    base = "".join(f"line {i}\n" for i in range(100))
    ours = base.replace("line 10\n", "ours 10\n").replace("line 50\n", "ours 50\n")
    theirs = base.replace("line 30\n", "theirs 30\n").replace("line 80\n", "theirs 80\n")
    r = merge3(base, ours, theirs)
    assert r.clean
    for s in ("ours 10", "ours 50", "theirs 30", "theirs 80"):
        assert s in r.text
    assert r.text.count("\n") == 100


# ------------------------------------------------------------------ branch merges

@pytest.fixture
def m(server):
    dev = server.signup()
    a = dev.provision_agent("mg", access="owner", storage_mb=100)
    return Modern(server, a["token"]["token"])


def test_branch_merge_combines_edits_to_the_same_file(m):
    m.call("cloud_write", path="/doc.md", content=BASE)
    br = m.call("cloud_branch", action="create", name="e")[0]["structuredContent"]["branch"]
    m.call("cloud_write", path="/doc.md", content=BASE.replace("f\n", "F-branch\n"), workspace=br)
    m.call("cloud_write", path="/doc.md", content=BASE.replace("b\n", "B-main\n"))
    _, t = m.call("cloud_branch", action="preview", workspace=br)
    assert "Conflicts" not in t and "auto-merge" in t
    res, t = m.call("cloud_branch", action="merge", workspace=br)
    assert not res["isError"], t
    assert m.call("cloud_read", path="/doc.md")[1] == "a\nB-main\nc\nd\ne\nF-branch\ng\n"
    assert res["structuredContent"]["auto_merged"] == ["doc.md"]


def test_branch_merge_conflict_reports_hunks_and_accepts_content(m):
    m.call("cloud_write", path="/doc.md", content=BASE)
    br = m.call("cloud_branch", action="create", name="c")[0]["structuredContent"]["branch"]
    m.call("cloud_write", path="/doc.md", content=BASE.replace("d\n", "D-branch\n"), workspace=br)
    m.call("cloud_write", path="/doc.md", content=BASE.replace("d\n", "D-main\n"))
    res, t = m.call("cloud_branch", action="merge", workspace=br)
    assert res["isError"]
    conflict = res["structuredContent"]["conflicts"][0]
    assert conflict["path"] == "/doc.md" and conflict["hunks"][0]["ours"] == "D-main\n"
    assert "<<<<<<<" in conflict["merged_with_markers"]
    res, t = m.call("cloud_branch", action="merge", workspace=br,
                    resolve={"/doc.md": {"content": BASE.replace("d\n", "D-both\n")}})
    assert not res["isError"], t
    assert m.call("cloud_read", path="/doc.md")[1] == BASE.replace("d\n", "D-both\n")


def test_binary_conflicts_still_need_ours_or_theirs(m):
    import base64
    m.call("cloud_write", path="/img.png", content=base64.b64encode(b"\x89PNG\x00base").decode(), encoding="base64")
    br = m.call("cloud_branch", action="create", name="b")[0]["structuredContent"]["branch"]
    m.call("cloud_write", path="/img.png", content=base64.b64encode(b"\x89PNG\x00br").decode(), encoding="base64", workspace=br)
    m.call("cloud_write", path="/img.png", content=base64.b64encode(b"\x89PNG\x00mn").decode(), encoding="base64")
    res, _ = m.call("cloud_branch", action="merge", workspace=br)
    assert res["isError"] and res["structuredContent"]["conflicts"][0].get("hunks") is None
    res, _ = m.call("cloud_branch", action="merge", workspace=br, resolve={"/img.png": "theirs"})
    assert not res["isError"]
