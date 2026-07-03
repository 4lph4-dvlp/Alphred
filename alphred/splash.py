"""전용 TUI 시작 화면용 ASCII 배너/로고(§13.4, 반응형).

과거 브랜딩(§10, 폐기)에 쓰던 `Alphred_Banner.txt` 아트를 재활용한다. 화면 폭에 따라
full(ALPHRED - AGENT) / half(ALPHRED) / mini(A) 세 변형을 골라 짤림을 막고, 세로
그라데이션(앰버→마룬, §10.7 "Alph-RED" 팔레트)을 줄별로 입혀 렌더한다.
"""
from __future__ import annotations

from pathlib import Path

from rich.text import Text

_ASSETS = Path(__file__).resolve().parent / "assets"

# "ALPHRED  -  AGENT" — 박스드로잉 ASCII 아트(원본: alphred/branding/assets/Alphred_Banner.txt)
_BANNER = r"""
 █████╗ ██╗     ██████╗ ██╗  ██╗██████╗ ███████╗██████╗                █████╗  ██████╗ ███████╗███╗   ██╗████████╗
██╔══██╗██║     ██╔══██╗██║  ██║██╔══██╗██╔════╝██╔══██╗              ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝
███████║██║     ██████╔╝███████║██████╔╝█████╗  ██║  ██║    █████╗    ███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║
██╔══██║██║     ██╔═══╝ ██╔══██║██╔══██╗██╔══╝  ██║  ██║    ╚════╝    ██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║
██║  ██║███████╗██║     ██║  ██║██║  ██║███████╗██████╔╝              ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║
╚═╝  ╚═╝╚══════╝╚═╝     ╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚═════╝               ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝
""".strip("\n").splitlines()


def _slice(cols: int) -> list[str]:
    """배너 각 줄을 [0:cols] 로 잘라 변형을 만든다(단어 경계 컬럼은 사전 분석값)."""
    w = max(len(l) for l in _BANNER)
    return [l.ljust(w)[:cols].rstrip() for l in _BANNER]


# 단어 경계(전수 분석): ALPHRED=0~56, ' - '=60~66, AGENT=70~114.
_BANNER_FULL = list(_BANNER)              # ALPHRED - AGENT
_BANNER_HALF = _slice(57)                 # ALPHRED
_BANNER_MINI = _slice(8)                  # A

_W_FULL = max(len(l) for l in _BANNER_FULL)   # 114
_W_HALF = max(len(l) for l in _BANNER_HALF)   # 56
_W_MINI = max(len(l) for l in _BANNER_MINI)   # 8
_BANNER_W = _W_FULL                        # (하위호환: 기존 테스트 참조)

# 메인화면 엠블럼(로고) — 원본 Alphred_Logo.txt 아스키 아트(assets/ 에 패키징).
# 자산이 없을 때를 대비한 최소 폴백(솟은 삼각 = 최우선 + 선점 큐).
_LOGO_FALLBACK = r"""
    █
   ███
  █████
 ███████
█████████
═════════
  █████
   ███
    █
""".strip("\n").splitlines()


def _normalize(lines: list[str]) -> list[str]:
    """상하 빈 줄 제거 + 좌측 공통 들여쓰기 제거(초상화 내부 정렬은 보존)."""
    lines = [ln.rstrip() for ln in lines]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return []
    indent = min((len(ln) - len(ln.lstrip(" ")) for ln in lines if ln.strip()), default=0)
    return [ln[indent:] for ln in lines]


def _load_logo(name: str) -> list[str]:
    """assets/<name> 로고 아트를 읽어 정규화한다. 없으면 폴백."""
    try:
        raw = (_ASSETS / name).read_text(encoding="utf-8").splitlines()
    except Exception:
        return _normalize(_LOGO_FALLBACK)
    out = _normalize(raw)
    return out or _normalize(_LOGO_FALLBACK)


# 배너처럼 화면 크기에 따라 고르는 로고 크기 변형(100% / 75% / 50%).
_LOGO_FULL = _load_logo("Alphred_Logo.txt")
_LOGO_75 = _load_logo("Alphred_Logo_75.txt")
_LOGO_50 = _load_logo("Alphred_Logo_50.txt")
_LOGO = _LOGO_FULL                         # (하위호환: 기존 참조)
_W_LOGO = max((len(l) for l in _LOGO), default=0)


def _logo_dims(lines: list[str]) -> tuple[int, int]:
    return (max((len(l) for l in lines), default=0), len(lines))


# 큰 것부터: (lines, width, height)
_LOGO_VARIANTS = [(v, *_logo_dims(v)) for v in (_LOGO_FULL, _LOGO_75, _LOGO_50) if v]

# §10.7 Alph-RED 그라데이션(밝음→어두움). 줄 수에 맞춰 균등 매핑.
_GRADIENT = ["#FF9F45", "#FF7A3D", "#FB5A3C", "#F03E41", "#E63946", "#D32F3C", "#B22232", "#8E1B28"]


def _gradient_lines(lines: list[str], *, center_to: int = 0) -> list[Text]:
    n = max(1, len(lines))
    out: list[Text] = []
    for i, line in enumerate(lines):
        color = _GRADIENT[min(len(_GRADIENT) - 1, i * len(_GRADIENT) // n)]
        if center_to:
            pad = max(0, (center_to - len(line)) // 2)
            line = " " * pad + line
        out.append(Text(line, style=f"bold {color}", no_wrap=True))
    return out


def pick_banner(avail_width: int) -> tuple[list[str], int]:
    """가용 폭에 맞는 배너 변형과 그 폭을 고른다(짤림 방지)."""
    if avail_width >= _W_FULL:
        return _BANNER_FULL, _W_FULL
    if avail_width >= _W_HALF:
        return _BANNER_HALF, _W_HALF
    return _BANNER_MINI, _W_MINI


def banner_lines(avail_width: int | None = None) -> list[Text]:
    """그라데이션 배너 줄들. avail_width 미지정 시 full(하위호환)."""
    raw, _w = pick_banner(avail_width if avail_width is not None else _W_FULL)
    return _gradient_lines(raw)


def _gradient_block(lines: list[str], *, center_to: int = 0) -> list[Text]:
    """블록 전체에 동일한 좌측 패딩을 적용해 가운데 정렬(초상화 정렬 보존) + 세로 그라데이션."""
    n = max(1, len(lines))
    block_w = max((len(l) for l in lines), default=0)
    pad = max(0, (center_to - block_w) // 2) if center_to else 0
    out: list[Text] = []
    for i, line in enumerate(lines):
        color = _GRADIENT[min(len(_GRADIENT) - 1, i * len(_GRADIENT) // n)]
        out.append(Text(" " * pad + line, style=f"bold {color}", no_wrap=True))
    return out


def pick_logo(avail_width: int, avail_height: int) -> tuple[list[str], int, int]:
    """가용 폭·높이에 모두 들어가는 가장 큰 로고 변형을 고른다(100%→75%→50%).

    배너의 pick_banner 와 같은 전략. 어느 것도 안 들어가면 ([],0,0) → 로고 생략.
    """
    for lines, w, h in _LOGO_VARIANTS:
        if avail_width >= w and avail_height >= h:
            return lines, w, h
    return [], 0, 0


def logo_lines(center_to: int | None = None, avail_height: int | None = None) -> list[Text]:
    """블록 가운데 정렬된 로고(엠블럼) 줄들 — 폭/높이에 맞는 변형 자동 선택.

    avail_height 미지정 시 높이 제약 없음(가장 큰 변형). 들어갈 변형이 없으면 빈 리스트.
    """
    target = center_to if center_to is not None else _W_FULL
    lines, w, _h = pick_logo(target or _W_FULL,
                             avail_height if avail_height is not None else 10**9)
    if not lines:
        return []
    return _gradient_block(lines, center_to=target)
