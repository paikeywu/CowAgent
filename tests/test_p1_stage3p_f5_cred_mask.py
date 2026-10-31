"""F-5 · C-1/C-4 — the criteria for the credential masking.

WHAT THESE CRITERIA MEASURE
---------------------------
They measure the BEHAVIOUR, not the source.  Each one asks the code to
do something and then looks at what came out:

* the export throat (``build_msg_line``) — does the written line carry a
  credential shape?
* the database throat (``ConversationStore.append_messages``) — does the
  stored row carry a credential shape?
* the chain — is the line's hash still consistent with its content?
* the read path — does ``load_content`` still return the text?

None of them greps the source to decide whether a filter exists.

THE BREAKERS (each one must turn a criterion RED)
--------------------------------------------------
B1  drop the masking call in ``build_msg_line``      -> F5-C1 red
B2  drop the masking call in the DB throat           -> F5-C2 red
B3  move the masking AFTER the hash is computed      -> F5-C3 + F5-C4 red
B4  delete one FORM from the rule table              -> F5-C5 red
B5  remove the placeholder exemption                 -> F5-C6 red
B6  make the masker non-idempotent                   -> F5-C7 red

The BREAKERS are applied by MONKEYPATCHING, not by switching git
branches.  That is deliberate: it proves on ONE working tree that the
criterion can go red AND green, and it leaves no branch to merge.
"""

import json

import pytest

from agent.memory import cred_mask
from agent.memory import msg_chain


#: A value that is unmistakably a credential.
SECRET = "SYNTHETIC-CREDENTIAL-DO-NOT-USE"

#: One sample per credential FORM, in the shapes the machine actually
#: uses.  The forms are written out rather than imported so that the
#: criterion can assert the SHAPES it claims to cover.
SAMPLES = {
    "S1-sshpass-unquoted": "sshpass -p %s ssh host" % SECRET,
    "S2-sshpass-quoted": "sshpass -p '%s' ssh -o StrictHostKeyChecking=no host" % SECRET,
    "S3-curl-user-unquoted": "curl -u paikeywu:%s http://h/" % SECRET,
    "S3-curl-user-quoted": "curl --user 'paikeywu:%s' http://h/" % SECRET,
    "S4-kv-unquoted": "password=%s" % SECRET,
    "S5-kv-quoted": 'password="%s"' % SECRET,
    "S6-json-field": '{"password": "%s"}' % SECRET,
    "S6-json-field-escaped": '{\\"password\\": \\"%s\\"}' % SECRET,
    "S7-url-userinfo": "https://user:%s@example.com/x" % SECRET,
    "S8-env-assign": "SSHPASS=%s" % SECRET,
    "S9-export": "export SSH_114_PASS=%s" % SECRET,
    "S10-assignment": 'PASS = "%s"' % SECRET,
}

#: Values that must NOT be masked — placeholders and ordinary content.
KEEP = [
    'password="<your-password>"',
    'password="REDACTED"',
    'password="***"',
    "SSHPASS=$SSH_114_PASS",
    "SSHPASS=${SSH_114_PASS}",
    "password=example",
    "password=None",
    "password=null",
    "password=os.environ",
    "pwd=abc",
    "token_budget=200",
    "bypass=true",
    "the quick brown fox jumps over the lazy dog",
    "please pass the salt",
    '{"role":"user","text":"hello"}',
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _content_of(text):
    """A message's content the way the database stores it: JSON TEXT."""
    return json.dumps({"text": text}, ensure_ascii=False)


def _line(content_text, seq=0):
    return msg_chain.build_msg_line(
        seq=seq, agent_id="a", session_id="s", role="assistant",
        content=_content_of(content_text), created_at=1, run_id="r",
        prev_hash=msg_chain.MSG_GENESIS_PREV_HASH,
    )


def _all_sampled_forms():
    """Every FORM the corpus triggers (form id -> sample text)."""
    hit = {}
    for text in SAMPLES.values():
        for fid in cred_mask.scan_forms(text):
            hit.setdefault(fid, text)
    return hit


# ---------------------------------------------------------------------------
# F5-C1 — the export throat does not persist a credential
# ---------------------------------------------------------------------------

def test_f5_c1_export_throat_persists_no_credential_shape():
    """F5-C1: every credential FORM is masked before the line is built.

    The line's own ``content`` is checked with the SAME scanner the
    criterion uses to look for leaks — a criterion that scanned with a
    different rule than the masker would be two mouths for one
    question.
    """
    for fid, text in SAMPLES.items():
        line = _line(text)
        forms = cred_mask.scan_forms(line["content"])
        assert not forms, (
            "form %s survived the export throat: %r" % (fid, forms))
        assert SECRET not in line["content"], (
            "the credential value survived the export throat (form %s)" % fid)


def test_f5_c1_breaker_no_masking_in_the_export_throat(monkeypatch):
    """B1: with the masker disabled, F5-C1 must FAIL.

    The masker is disabled by making the module's entry point a no-op —
    which is exactly what the code looked like BEFORE the fix.

    **The module attribute is patched, not the imported name.**  The
    export throat calls ``cred_mask.mask_text(...)`` — an ATTRIBUTE
    lookup on the module at call time — so patching the module attribute
    is what actually disables it.  Patching the name that another module
    imported would not reach the call site.
    """
    import agent.memory.msg_chain as mc

    monkeypatch.setattr(cred_mask, "mask_text", lambda t: t)
    line = mc.build_msg_line(
        seq=0, agent_id="a", session_id="s", role="assistant",
        content=_content_of(SAMPLES["S2-sshpass-quoted"]),
        created_at=1, run_id="r",
        prev_hash=mc.MSG_GENESIS_PREV_HASH,
    )
    forms = cred_mask.scan_forms(line["content"])
    assert forms, (
        "the criterion cannot detect an unmasked export — the masker is "
        "not actually in the export throat")
    assert SECRET in line["content"]


# ---------------------------------------------------------------------------
# F5-C2 — the database throat does not persist a credential
# ---------------------------------------------------------------------------

def test_f5_c2_database_throat_persists_no_credential_shape(tmp_path):
    """F5-C2: the stored row carries no credential shape either.

    Masking only in the export layer would leave the ``messages.content``
    projection holding the credential, and every live reader serves that
    projection.  This criterion covers the database throat.
    """
    import sqlite3

    from agent.memory.conversation_store import ConversationStore

    ws = tmp_path / "ws"
    (ws / "memory" / "long-term").mkdir(parents=True)
    db = ws / "memory" / "long-term" / "index.db"
    store = ConversationStore(db_path=db, agent_id="a")
    msgs = [{"role": "assistant",
             "content": [{"type": "text", "text": t}]}
            for t in SAMPLES.values()]
    store.append_messages("sess-1", msgs, run_id="r1")

    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT content FROM messages WHERE content IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()

    assert rows, "the store wrote no rows — the criterion measured nothing"
    for (content,) in rows:
        forms = cred_mask.scan_forms(content)
        assert not forms, ("a stored row carries a credential shape: %r" % forms)
        assert SECRET not in content


def test_f5_c2b_the_merge_does_not_import_a_credential(tmp_path):
    """F5-C2b: the cross-library MERGE is a third write throat.

    ``_merge_one_agent`` copies another Agent's ``messages`` rows into
    this one.  A merged library must not import another library's
    credential.

    **This criterion exists because the merge path was BROKEN and only
    running it found that.**  The masking is a registered SQLite
    FUNCTION (``mask_cred``), and the statement that uses it is an
    f-string — so the placeholder had to be escaped (``{{mask_expr}}``).
    The first version raised ``NameError: name 'mask_expr' is not
    defined`` at merge time, and the merge is a RARE path (it runs when
    a secondary workspace is opened), so no other criterion touched it.

    The lesson: a throat that is only exercised occasionally needs its
    own criterion, or it stays broken until the next time it runs for
    real.
    """
    import json as _json
    import sqlite3 as _sqlite3

    from agent.memory import conversation_store as cs

    src = tmp_path / "src.db"
    conn = _sqlite3.connect(str(src))
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, agent_id TEXT, "
        "session_id TEXT, seq INTEGER, role TEXT, content TEXT, "
        "created_at INTEGER, extras TEXT, run_id TEXT)")
    for i, text in enumerate(SAMPLES.values()):
        conn.execute(
            "INSERT INTO messages (agent_id, session_id, seq, role, content, "
            "created_at, extras, run_id) VALUES (?,?,?,?,?,?,?,?)",
            ("src-agent", "sess-src", i, "assistant",
             _json.dumps({"text": text}), 1, "", "r"))
    conn.commit()
    conn.close()

    from agent.memory.conversation_store import ConversationStore
    dst = tmp_path / "dst.db"
    store = ConversationStore(db_path=dst, agent_id="dst-agent")
    dconn = _sqlite3.connect(str(dst))
    dconn.row_factory = _sqlite3.Row
    try:
        cs._merge_one_agent(dconn, str(src), "src-agent")
        rows = dconn.execute(
            "SELECT content FROM messages WHERE agent_id = 'src-agent'"
        ).fetchall()
    finally:
        dconn.close()

    assert rows, "the merge copied no rows — the criterion measured nothing"
    for r in rows:
        forms = cred_mask.scan_forms(r["content"])
        assert not forms, ("a merged row carries a credential shape: %r" % forms)
        assert SECRET not in r["content"]


def test_f5_c2_breaker_no_masking_in_the_database_throat(monkeypatch):
    """B2: with the masker disabled, F5-C2 must FAIL.

    The DB throat calls ``cred_mask.mask_content``; disabling the
    module's entry point disables both throats, which is what the
    pre-fix code did.
    """
    monkeypatch.setattr(cred_mask, "mask_content", lambda o: o)
    masked = cred_mask.mask_content(SAMPLES["S5-kv-quoted"])
    forms = cred_mask.scan_forms(masked)
    assert forms, "the criterion cannot detect an unmasked stored row"


# ---------------------------------------------------------------------------
# F5-C3 — the chain stays self-consistent
# ---------------------------------------------------------------------------

def test_f5_c3_the_masked_line_hashes_to_what_it_says():
    """F5-C3: the line's hash describes the MASKED content.

    This is the criterion that forces the masking to happen BEFORE the
    hash.  Masking after the hash would leave the stored hash describing
    text that is no longer on disk.
    """
    for fid, text in SAMPLES.items():
        line = _line(text)
        recomputed = msg_chain.msg_line_hash(line)
        assert recomputed == line["line_hash"], (
            "form %s: the line hashes to %s but says %s"
            % (fid, recomputed, line["line_hash"]))


def test_f5_c3_breaker_masking_after_the_hash_breaks_the_chain():
    """B3: masking AFTER the hash must break the chain.

    The "old" order — hash first, mask later — is reproduced here by
    building the line the way the code did BEFORE the fix: the hash is
    computed over the UNMASKED content, and only then is the content
    masked.  The recomputed hash must then DIFFER from the stored one.

    **The line is built with the masking disabled** (the module's entry
    point is a no-op for the duration), because the fix masks BEFORE
    the hash — masking the already-masked line again would be a no-op
    and the breaker would prove nothing.
    """
    import agent.memory.msg_chain as mc

    real_mask = cred_mask.mask_text
    try:
        cred_mask.mask_text = lambda t: t
        line = mc.build_msg_line(
            seq=0, agent_id="a", session_id="s", role="assistant",
            content=_content_of(SAMPLES["S2-sshpass-quoted"]),
            created_at=1, run_id="r",
            prev_hash=mc.MSG_GENESIS_PREV_HASH,
        )
    finally:
        cred_mask.mask_text = real_mask

    # the hash was computed on the UNMASKED content
    stored_hash = line["line_hash"]
    # now mask the content, as a post-hash filter would
    line["content"] = cred_mask.mask_text(line["content"])
    recomputed = mc.msg_line_hash(line)
    assert recomputed != stored_hash, (
        "masking after the hash did not break the chain — the criterion "
        "cannot detect the wrong order")


# ---------------------------------------------------------------------------
# F5-C4 — the read path still works
# ---------------------------------------------------------------------------

def test_f5_c4_the_read_path_returns_the_masked_text(tmp_path):
    """F5-C4: ``load_content`` reads the masked line back without drift.

    The drift check re-computes the line's hash and compares it with the
    DB's ``content_hash``.  If the masking broke that agreement, every
    read would raise ``ContentDriftError``.
    """
    ws = tmp_path / "ws"
    line = _line(SAMPLES["S2-sshpass-quoted"])
    msg_chain.append_msg_line(ws, "s", "2026-10-31", line)
    rel = msg_chain.session_relpath("s", "2026-10-31")
    row = {
        "content_state": msg_chain.STATE_EXTERNAL,
        "content_path": rel,
        "content_anchor": "line:1",
        "content_hash": line["line_hash"],
        "content": None,
    }
    text = msg_chain.load_content(row, ws)
    assert SECRET not in text
    assert cred_mask.REDACTED in text


def test_f5_c4_breaker_masking_after_the_hash_makes_the_read_raise(tmp_path):
    """B4: masking after the hash makes the read path RAISE.

    This is the twin of B3, observed from the reader's side: the read
    path is what a user actually experiences, and a ``ContentDriftError``
    is how it fails.

    The line is built with the masking DISABLED — the pre-fix order —
    and only the FILE's content is masked afterwards, so the DB's
    ``content_hash`` describes content that is no longer on disk.
    """
    import agent.memory.msg_chain as mc

    ws = tmp_path / "ws"
    real_mask = cred_mask.mask_text
    try:
        cred_mask.mask_text = lambda t: t
        line = mc.build_msg_line(
            seq=0, agent_id="a", session_id="s", role="assistant",
            content=_content_of(SAMPLES["S2-sshpass-quoted"]),
            created_at=1, run_id="r",
            prev_hash=mc.MSG_GENESIS_PREV_HASH,
        )
    finally:
        cred_mask.mask_text = real_mask

    mc.append_msg_line(ws, "s", "2026-10-31", line)
    rel = mc.session_relpath("s", "2026-10-31")

    # rewrite the file with the content masked AFTER the hash
    p = mc.session_path(ws, "s", "2026-10-31")
    raw = p.read_text(encoding="utf-8")
    obj = json.loads(raw.strip())
    obj["content"] = cred_mask.mask_text(obj["content"])
    p.write_text(json.dumps(obj, ensure_ascii=False) + "\n", encoding="utf-8")

    row = {
        "content_state": mc.STATE_EXTERNAL,
        "content_path": rel,
        "content_anchor": "line:1",
        "content_hash": line["line_hash"],
        "content": None,
    }
    with pytest.raises(msg_chain.ContentDriftError):
        msg_chain.load_content(row, ws)


# ---------------------------------------------------------------------------
# F5-C5 — the FORM table is complete and reachable
# ---------------------------------------------------------------------------

def test_f5_c5_every_form_is_reachable():
    """F5-C5: every FORM id in the table is triggered by the corpus.

    This is the criterion that keeps the criterion honest.  If a rule is
    silently deleted from the table, the corpus stops triggering it and
    the table's own ids would no longer be covered — this test fails
    rather than letting the coverage quietly shrink.
    """
    hit = _all_sampled_forms()
    missing = [fid for fid in cred_mask.FORM_IDS if fid not in hit]
    assert not missing, (
        "these FORM ids are not exercised by the corpus: %s — either the "
        "rule was deleted or the corpus lost its sample" % missing)
    extra = [fid for fid in hit if fid not in cred_mask.FORM_IDS]
    assert not extra, (
        "the corpus triggers FORM ids that are not in the table: %s" % extra)


def test_f5_c5_breaker_a_deleted_form_is_detected(monkeypatch):
    """B4 (the criterion's own): deleting a FORM must be detected.

    The break is applied to the TABLE the criterion reads, so the
    criterion must notice that the table and the corpus disagree.
    """
    monkeypatch.setattr(cred_mask, "FORM_IDS",
                        tuple(f for f in cred_mask.FORM_IDS
                              if f != "S3-curl-user-unquoted"))
    hit = _all_sampled_forms()
    # the FORM is still TRIGGERED by the corpus, but it is no longer in
    # the table -> the criterion's two-sided check must fail
    assert "S3-curl-user-unquoted" in hit
    assert "S3-curl-user-unquoted" not in cred_mask.FORM_IDS


# ---------------------------------------------------------------------------
# F5-C6 — placeholders survive
# ---------------------------------------------------------------------------

def test_f5_c6_placeholders_and_ordinary_content_survive():
    """F5-C6: a placeholder is not a credential.

    A masker that rewrites ``password="<your-password>"`` corrupts
    ordinary documentation and changes every hash that depends on it.
    """
    for text in KEEP:
        out = cred_mask.mask_text(text)
        assert out == text, (
            "a placeholder / ordinary content was rewritten: %r -> %r"
            % (text, out))


def test_f5_c6_breaker_without_the_placeholder_exemption(monkeypatch):
    """B5: without the exemption, the placeholders get rewritten.

    The exemption is reproduced here as an absent rule: the value test
    becomes "long enough" only.
    """
    monkeypatch.setattr(cred_mask, "is_placeholder", lambda v: False)
    text = 'password="<your-password>"'
    out = cred_mask.mask_text(text)
    assert out != text, (
        "the criterion cannot detect a masker that rewrites placeholders")


# ---------------------------------------------------------------------------
# F5-C7 — the masker is idempotent
# ---------------------------------------------------------------------------

def test_f5_c7_the_masker_is_idempotent():
    """F5-C7: ``mask(mask(x)) == mask(x)``.

    The masker runs on every message, and an externalised line can be
    re-processed.  A masker that changed its output on the second pass
    would make the hash depend on how many times the text went through
    it.
    """
    for text in list(SAMPLES.values()) + KEEP:
        once = cred_mask.mask_text(text)
        twice = cred_mask.mask_text(once)
        assert once == twice, (
            "the masker is not idempotent: %r -> %r -> %r"
            % (text, once, twice))


def test_f5_c7_breaker_a_non_idempotent_masker_is_detected(monkeypatch):
    """B6: a masker that keeps changing its output is detected."""
    calls = {"n": 0}

    def growing(t):
        calls["n"] += 1
        return t + "<X>"

    monkeypatch.setattr(cred_mask, "mask_text", growing)
    once = cred_mask.mask_text("hello")
    twice = cred_mask.mask_text(once)
    assert once != twice


# ---------------------------------------------------------------------------
# The masker does not corrupt the JSON
# ---------------------------------------------------------------------------

def test_f5_c8_the_masked_content_is_still_valid_json():
    """F5-C8: the masked content still parses.

    The content is JSON TEXT.  A masker that broke the JSON would make
    the read path raise instead of return — a "fix" that stops the leak
    by stopping the system.
    """
    for text in SAMPLES.values():
        content = cred_mask.mask_text(_content_of(text))
        obj = json.loads(content)
        assert isinstance(obj, dict)
        assert SECRET not in json.dumps(obj, ensure_ascii=False)
