// 사이트 전역 상단 네비게이션 링크의 단일 소스(2026-09-08 신설).
//
// 사용자 지적: "소개, 개인정보처리방침, 이용약관 등을 들어가면 카테고리가
// 현재 사용하는 것과 다르다" — privacy.html/terms.html이 카테고리 필터
// 링크 도입 이전의 예전 네비를 그대로 갖고 있었다. "페이지를 하나하나 다
// 만들어서 생기는 일 같은데, 카테고리 페이지를 하나 만들어서 다른데서는
// 받아다 쓰는 방식으로 바꾸면 되지 않나?" — image-credit.js/update-log-
// filter.js와 같은 이유로 nav 링크 목록도 여기 하나로 모은다. 각 페이지는
// 이제 <a> 태그를 직접 하드코딩하지 않고 renderPageNav()를 호출해 채운다.
export const CATEGORIES = [
  '경제', '금융', '자원·에너지', '산업·기업', '정치·외교', '사회', 'IT·과학',
];

export const UTILITY_LINKS = [
  { href: '/archive.html', label: '아카이브' },
  { href: '/calendar.html', label: '경제 일정' },
  { href: '/markets.html', label: '마켓' },
  { href: '/country.html', label: '국가별' },
  { href: '/weather.html', label: '날씨' },
];

// 다국어(글로벌) 채널 로고옆 진입 배지(renderLogoGlobalLink)는 2026-09-10
// 제거함 — 힌디어 번역 한글 유출 재발 등 품질 문제가 반복돼 공개 페이지
// (docs/global/*)를 통째로 내리고 내부 검증만 계속하기로 함(사용자 지시:
// "배지를 끄라는게 아니라 제거해"). /global/ 페이지·번역 파이프라인 자체는
// 계속 돌며 내부 검증 중 — 품질이 검증되면 이 커밋에서 지운 배지를 다시
// 추가하면 된다(git 히스토리에 보존됨).

/**
 * 상단 page-nav(플랫 바 형태) 링크 목록을 렌더링해 mountEl에 채운다.
 * activeHref로 현재 페이지 링크에 "active" 클래스를 붙인다.
 * liveLabel/liveHref로 "트렌드" 링크의 표기(이모지·강조색 등)를 페이지별로
 * 다르게 줄 수 있다 — 내용(카테고리 목록)만 공용화하고 각 페이지 고유의
 * 강조 스타일은 그대로 살린다.
 */
export function renderPageNav(mountEl, opts = {}) {
  if (!mountEl) return;
  const {
    activeHref = '',
    liveLabel = '📈 트렌드',
    liveHtml = '',
    includeHome = true,
    extraLinks = [],
  } = opts;

  const links = [];
  if (includeHome) links.push({ href: '/', label: '홈' });
  links.push({ href: '/live.html', label: liveHtml || liveLabel, raw: !!liveHtml });
  for (const c of CATEGORIES) {
    links.push({ href: `/?category=${encodeURIComponent(c)}`, label: c });
  }
  links.push(...UTILITY_LINKS);
  links.push(...extraLinks);

  mountEl.innerHTML = links.map(l => {
    const cls = l.href === activeHref ? ' class="active"' : '';
    const label = l.raw ? l.label : escapeHtml(l.label);
    return `<a href="${l.href}"${cls}>${label}</a>`;
  }).join('');
}

/**
 * sector-dropdown(버튼+드롭다운) 형태 nav-sector용 — index.html의 setSector()
 * 인터랙션(선택 상태 유지, 필터 갱신)은 그대로 두고 카테고리 목록 데이터만
 * 공유한다. onclick 핸들러 문자열을 직접 만들어 넣는다.
 */
export function renderSectorNav(mountEl, opts = {}) {
  if (!mountEl) return;
  const { includeAll = true, activeAll = false, activeHref = '' } = opts;
  const parts = [];
  parts.push(`<a href="/live.html" style="color:var(--accent2);font-weight:700;">트렌드</a>`);
  if (includeAll) {
    parts.push(`<a href="#" class="${activeAll ? 'active' : ''}" onclick="setSector('all',this);return false;">전체</a>`);
  } else {
    parts.push(`<a href="/">전체</a>`);
  }
  for (const c of CATEGORIES) {
    if (includeAll) {
      parts.push(`<a href="#" onclick="setSector('${c}',this);return false;" data-sector>${escapeHtml(c)}</a>`);
    } else {
      parts.push(`<a href="/?category=${encodeURIComponent(c)}">${escapeHtml(c)}</a>`);
    }
  }
  for (const l of UTILITY_LINKS) {
    const cls = l.href === activeHref ? ' class="active"' : '';
    parts.push(`<a href="${l.href}"${cls}>${l.label}</a>`);
  }
  mountEl.innerHTML = parts.join('\n');
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}
