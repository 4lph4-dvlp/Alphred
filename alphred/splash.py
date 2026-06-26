"""전용 TUI 시작 화면용 ASCII 배너/로고(§13.4, 반응형).

과거 브랜딩(§10, 폐기)에 쓰던 `Alphred_Banner.txt` 아트를 재활용한다. 화면 폭에 따라
full(ALPHRED - AGENT) / half(ALPHRED) / mini(A) 세 변형을 골라 짤림을 막고, 세로
그라데이션(앰버→마룬, §10.7 "Alph-RED" 팔레트)을 줄별로 입혀 렌더한다.
"""
from __future__ import annotations

from rich.text import Text

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

# 메인화면 엠블럼(로고) — 원본 Alphred_Logo.txt(브랜딩 폐기와 함께 삭제됨) 대체.
# 솟은 삼각(최우선) + 그 아래로 짧아지는 막대(우선순위 큐/선점 스케줄러)를 형상화.
# 배너와 동일하게 █ 와 ═(박스드로잉)만 사용 → 터미널/폰트 호환(깨짐 방지).
_LOGO = r"""
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
_W_LOGO = max(len(l) for l in _LOGO)

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


def logo_lines(center_to: int | None = None) -> list[Text]:
    """배너(또는 지정) 폭 기준 가운데 정렬된 로고(엠블럼) 줄들."""
    return _gradient_lines(_LOGO, center_to=center_to if center_to is not None else _W_FULL)
