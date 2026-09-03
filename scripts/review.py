#!/usr/bin/env python3
"""pr-gate — PR diff를 에이전트 CLI에 태워 P1~P3 등급을 매기고 코멘트/라벨을 단다.

모델 SDK 의존성 없음. 리뷰 실행은 $REVIEW_CMD_SMALL / $REVIEW_CMD_BIG 문자열이라
claude 대신 codex·grok CLI로 갈아끼워도 이 파일은 그대로다.

머지는 하지 않는다. P1이 있으면 exit 1 -> check 실패 -> GitHub이 머지를 막는다.
"""
import json
import os
import re
import subprocess
import sys

MARKER = "<!-- pr-gate -->"
FOLLOWUP_RE = re.compile(r"<!-- pr-gate-followup:(\d+) -->")
DOCS_ONLY_RE = re.compile(r"(^docs/|\.md$)")
# 인증·시크릿·DB 경로는 변경 줄 수와 무관하게 큰 모델로 본다
SENSITIVE_RE = re.compile(
    r"(auth|login|secret|token|password|credential|payment|migration|schema|database|\bdb\b|\.env)", re.I
)
BIG_LINES = 100
MAX_DIFF_CHARS = 80000

P1_LABEL = "ai:p1-blocked"
P2_LABEL = "ai:p2-followup"


# --- GitHub CLI 얇은 래퍼 (gh 는 Actions 러너에 기본 설치돼 있다) ---
def gh(args, stdin=None):
    r = subprocess.run(["gh"] + args, input=stdin, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError("gh " + " ".join(args) + " 실패: " + r.stderr.strip())
    return r.stdout


def gh_json(args, stdin=None):
    return json.loads(gh(args, stdin) or "null")


# --- 티어 판정 ---
def classify(files, changed_lines):
    """files: 변경 파일 경로 목록 -> 'skip' | 'small' | 'big'"""
    if files and all(DOCS_ONLY_RE.search(f) for f in files):
        return "skip"
    if any(SENSITIVE_RE.search(f) for f in files):
        return "big"
    return "big" if changed_lines >= BIG_LINES else "small"


# --- 리뷰 실행 ---
PROMPT = """당신은 코드 리뷰어입니다. 아래 PR diff를 검토하고 발견 사항에 등급을 매기세요.

## 등급 기준
- P1: 동작하지 않거나 보안 구멍. 예) 널 참조 크래시, 시크릿 하드코딩, 인증 우회, SQL 인젝션, 데이터 유실
- P2: 버그 가능성 또는 운영 위험. 예) 경계조건 미처리, 예외를 조용히 삼킴, 레이스 컨디션, N+1 쿼리
- P3: 스타일·가독성. 예) 죽은 코드, 네이밍, 중복

확실하지 않으면 한 등급 낮게 매기세요. 억지로 찾지 마세요 — 발견 사항이 없으면 빈 배열이 정답입니다.

## 출력
아래 JSON만 출력하세요. 앞뒤에 설명이나 코드펜스를 붙이지 마세요.
{"summary": "한 줄 총평", "findings": [{"grade": "P1", "file": "경로", "line": 12, "why": "왜 문제인가", "fix": "어떻게 고치나"}]}

<diff>
%s
</diff>
"""

_FENCE_RE = re.compile(r"^```[a-z]*\n|\n```$", re.M)


def parse_review(raw):
    """모델 출력에서 JSON만 뽑는다. 코드펜스나 앞뒤 잡담이 붙어도 견딘다."""
    text = _FENCE_RE.sub("", raw.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("JSON을 찾을 수 없음: " + repr(raw[:200]))
    data = json.loads(text[start:end + 1])
    findings = [f for f in data.get("findings", []) if f.get("grade") in ("P1", "P2", "P3")]
    findings.sort(key=lambda f: f["grade"])
    return {"summary": data.get("summary", ""), "findings": findings}


# diff는 PR 작성자가 완전히 통제하는 신뢰할 수 없는 입력이다. 리뷰 CLI에게 Read 도구
# 권한이 있으므로, 부모 환경을 그대로 물려주면 diff 안의 프롬프트 인젝션이 /proc/self/environ을
# 읽게 만들어 GH_TOKEN 등을 findings 텍스트에 담아 공개 PR 코멘트로 유출시킬 수 있다.
# 화이트리스트만 넘긴다 — GH_TOKEN은 절대 여기 포함하지 않는다.
_REVIEW_ENV_ALLOWLIST = ("PATH", "HOME", "CLAUDE_CODE_OAUTH_TOKEN")


def run_review(cmd, diff):
    review_env = {k: os.environ[k] for k in _REVIEW_ENV_ALLOWLIST if k in os.environ}
    r = subprocess.run(
        cmd, shell=True, input=PROMPT % diff, capture_output=True, text=True, encoding="utf-8",
        timeout=600, env=review_env,
    )
    if r.returncode != 0:
        # 일부 CLI는 에러를 stderr가 아니라 stdout에 낸다 — 둘 다 남겨야 원인을 알 수 있다.
        detail = (r.stderr.strip() or r.stdout.strip())[:500]
        raise RuntimeError("리뷰 명령 실패 (exit %d, %s): %s" % (r.returncode, cmd, detail))
    return parse_review(r.stdout)


# --- 코멘트 렌더링 ---
def _md_cell(s):
    """모델이 생성한 텍스트를 마크다운 테이블 셀에 안전하게 넣는다.
    '|'나 개행이 그대로 들어가면 테이블이 깨지고 뒤 행이 잘못 파싱된다."""
    return str(s).replace("|", "\\|").replace("\r\n", " ").replace("\n", "<br>")


def render(review, tier, cmd, sha, followup=None):
    counts = {g: sum(1 for f in review["findings"] if f["grade"] == g) for g in ("P1", "P2", "P3")}
    lines = [MARKER, "## 🤖 pr-gate 리뷰", ""]
    lines.append("**티어** `" + tier + "` · **명령** `" + cmd + "` · **커밋** `" + sha[:7] + "`")
    lines.append("")
    if review["summary"]:
        lines += [_md_cell(review["summary"]).replace("<br>", "\n"), ""]
    if review["findings"]:
        lines += ["| 등급 | 위치 | 내용 |", "|---|---|---|"]
        for f in review["findings"]:
            loc = _md_cell(str(f.get("file", "?")) + ":" + str(f.get("line", "?")))
            lines.append("| **" + f["grade"] + "** | `" + loc + "` | "
                         + _md_cell(f.get("why", "")) + "<br>→ " + _md_cell(f.get("fix", "")) + " |")
        lines.append("")
    lines.append("**P1 %d · P2 %d · P3 %d**" % (counts["P1"], counts["P2"], counts["P3"]))
    if counts["P1"]:
        lines += ["", "⛔ P1이 있어 머지가 차단됩니다. 고친 뒤 커밋을 올리면 다시 검수합니다."]
    if followup:
        lines += ["", "후속 이슈: #%d" % followup, "<!-- pr-gate-followup:%d -->" % followup]
    return "\n".join(lines)


def render_too_big(files, size, sha):
    head = [
        MARKER,
        "## 🤖 pr-gate 리뷰",
        "",
        "**검수 생략 — diff가 %d자로 너무 큽니다** (상한 %d자) · 커밋 `%s`" % (size, MAX_DIFF_CHARS, sha[:7]),
        "",
        "리뷰 가능한 논리 단위로 PR을 쪼개주세요. 변경된 파일:",
        "",
    ]
    return "\n".join(head + ["- `" + f + "`" for f in files[:50]])


# --- 코멘트 upsert (마커로 찾아 갱신 — 커밋을 더 올려도 코멘트가 쌓이지 않는다) ---
def find_comment(repo, pr):
    for c in gh_json(["api", "repos/%s/issues/%d/comments" % (repo, pr), "--paginate"]) or []:
        if MARKER in (c.get("body") or ""):
            return c
    return None


# 마지막 방어선: 리뷰 CLI가 diff 프롬프트 인젝션으로 무엇을 읽어냈든(자기 인증 토큰,
# .git/config, 다른 파일), 그 값이 시크릿 모양이면 공개 코멘트로 나가기 직전에 지운다.
# 입력 경로를 하나씩 막는 것(env 화이트리스트, persist-credentials)은 계속 새는 구멍이
# 나올 수 있지만, 출력을 막으면 경로가 몇 개든 상관없다. (~/.claude/CLAUDE.md 시크릿
# 스캔 패턴과 동일 계열: 주요 클라우드/깃 플랫폼 키 접두사 + 명시적 자격증명 대입)
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"gh[ps]_[A-Za-z0-9]{20,}"),
    re.compile(r"glpat-[A-Za-z0-9_-]{15,}"),
    re.compile(r"AKIA[0-9A-Z]{12,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"(?i)(password|secret|token|api[_-]?key)['\"]?\s*[:=]\s*['\"]?[^\s'\"]{8,}"),
]


def redact_secrets(text):
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def upsert_comment(repo, pr, body, existing):
    body = redact_secrets(body)
    payload = json.dumps({"body": body})
    if existing:
        gh(["api", "-X", "PATCH", "repos/%s/issues/comments/%d" % (repo, existing["id"]), "--input", "-"],
           stdin=payload)
    else:
        gh(["api", "-X", "POST", "repos/%s/issues/%d/comments" % (repo, pr), "--input", "-"], stdin=payload)


def ensure_labels(repo):
    """P1/P2 라벨이 레포에 없으면 만든다. create_followup()이 라벨을 참조하기 전에
    반드시 먼저 호출해야 한다 — 순서가 바뀌면 'label not found'로 이슈 생성이 실패한다."""
    for name, color in ((P1_LABEL, "d73a4a"), (P2_LABEL, "fbca04")):
        subprocess.run(["gh", "label", "create", name, "--color", color, "--force", "--repo", repo],
                       capture_output=True, text=True)


def set_labels(repo, pr, want):
    """want 에 있는 라벨만 남긴다. 재실행으로 등급이 사라지면 라벨도 뗀다."""
    for name in (P1_LABEL, P2_LABEL):
        flag = "--add-label" if name in want else "--remove-label"
        subprocess.run(["gh", "issue", "edit", str(pr), flag, name, "--repo", repo],
                       capture_output=True, text=True)


def create_followup(repo, pr, findings):
    body = ["PR #%d 리뷰에서 나온 P2 후속 항목입니다.\n" % pr]
    for f in findings:
        body.append("- [ ] `%s:%s` — %s → %s" % (f.get("file", "?"), f.get("line", "?"),
                                                 f.get("why", ""), f.get("fix", "")))
    out = gh(["issue", "create", "--repo", repo, "--title", "P2 후속: PR #%d 리뷰 지적사항" % pr,
              "--label", P2_LABEL, "--body-file", "-"], stdin=redact_secrets("\n".join(body)))
    m = re.search(r"/issues/(\d+)", out)
    return int(m.group(1)) if m else None


def main():
    repo, pr, sha = os.environ["REPO"], int(os.environ["PR_NUMBER"]), os.environ["HEAD_SHA"]

    meta = gh_json(["pr", "view", str(pr), "--repo", repo, "--json", "files"])
    files = [f["path"] for f in meta["files"]]
    changed = sum(f["additions"] + f["deletions"] for f in meta["files"])

    tier = classify(files, changed)
    print("[pr-gate] 파일 %d개 / %d줄 변경 -> 티어 %s" % (len(files), changed, tier))
    if tier == "skip":
        print("[pr-gate] 문서만 변경 — 리뷰 생략 (한도 소모 0)")
        return 0

    ensure_labels(repo)
    existing = find_comment(repo, pr)
    prior = FOLLOWUP_RE.search(existing["body"]) if existing else None
    followup = int(prior.group(1)) if prior else None

    diff = gh(["pr", "diff", str(pr), "--repo", repo])
    if len(diff) > MAX_DIFF_CHARS:
        upsert_comment(repo, pr, render_too_big(files, len(diff), sha), existing)
        set_labels(repo, pr, {P1_LABEL})
        print("[pr-gate] diff %d자 — 상한 초과로 차단" % len(diff))
        return 1

    cmd = os.environ["REVIEW_CMD_BIG" if tier == "big" else "REVIEW_CMD_SMALL"]
    review = run_review(cmd, diff)

    p1 = [f for f in review["findings"] if f["grade"] == "P1"]
    p2 = [f for f in review["findings"] if f["grade"] == "P2"]

    # 멱등성: 이미 만든 후속 이슈 번호가 우리 코멘트에 마커로 남아 있으면 다시 만들지 않는다.
    # GitHub 검색 인덱싱 지연에 기대지 않는 방식 — 마커는 우리가 직접 쓴 코멘트 안에 있다.
    if p2 and followup is None:
        followup = create_followup(repo, pr, p2)

    upsert_comment(repo, pr, render(review, tier, cmd, sha, followup), existing)

    want = set()
    if p1:
        want.add(P1_LABEL)
    if p2 or followup:
        want.add(P2_LABEL)
    set_labels(repo, pr, want)

    print("[pr-gate] P1 %d · P2 %d · P3 %d"
          % (len(p1), len(p2), len(review["findings"]) - len(p1) - len(p2)))
    return 1 if p1 else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print("[pr-gate] 오류: %s" % e, file=sys.stderr)
        sys.exit(1)
