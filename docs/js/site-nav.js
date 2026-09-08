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

// 다국어(글로벌) 채널 진입점(2026-09-08 신설) — 사용자 지적: "사이트에 들어갈
// 수 있는 공간 같은게 보이질 않는데". 카테고리 nav(UTILITY_LINKS)에 넣었다가
// "아니 카테고리에 넣지 말고 메인 로고 옆에 두자니까?" 지적으로 로고 옆
// 전용 위치로 이동 — renderLogoGlobalLink()가 로고 바로 옆에 별도로 그린다.
// 국기 이모지(🇺🇸🇮🇳🇫🇷🇪🇸)는 "사이트가 쓸데없이 복잡해" 지적으로 단순 텍스트로
// 교체함. 언어별 선택(EN/HI/FR/ES 버튼)은 /global/index.html 안의 langSwitch가
// 담당하므로 여긴 진입점 하나만 있으면 된다. 서브도메인(global.newsfinal.co.kr)
// DNS 연결 전이라도 /global/은 같은 docs/ 배포 안에 있어 상대경로로 지금 바로
// 접근 가능 — 그래서 절대 URL이 아니라 상대경로를 쓴다.
export const GLOBAL_LINK = { href: '/global/', label: 'GLOBAL' };

/**
 * 로고 바로 옆에 다국어 채널 진입 링크를 작게 그린다. 페이지의 .logo
 * 옆에 mountEl(예: 빈 <span>)을 두고 호출하면 된다.
 */
export function renderLogoGlobalLink(mountEl) {
  if (!mountEl) return;
  mountEl.innerHTML = `<a href="${GLOBAL_LINK.href}" style="font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:700;color:var(--muted);text-decoration:none;border:1px solid var(--border);padding:3px 7px;letter-spacing:0.03em;">${GLOBAL_LINK.label}</a>`;
}

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
