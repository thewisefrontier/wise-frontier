// 기사 히어로 이미지 출처 라벨(2026-09-03 신설).
// docs/article.html과 functions/article.js 양쪽이 거의 동일한 이미지 출처
// 표시 로직을 각자 갖고 있었고, functions/article.js 쪽은 image_credit
// 필드(위키미디어 CC-BY 등 저작자 표기 의무가 있는 라이선스용, article_image.py
// fetch_wikimedia_image() 참조) 자체를 아예 안 보고 있어 두 곳이 서로
// 달랐다(update-log-filter.js와 같은 이유로 공용화 — docs/js/*.js는
// functions/article.js가 상대경로로 직접 import해 씀).
//
// image_credit이 있으면(저작자 표기 의무 있는 라이선스) 그대로 쓰고,
// 없으면(퍼블릭도메인 등 표기 의무 없음) URL 호스트로 추정해 출처만
// 밝힌다 — 법적 의무는 아니지만 이미지 출처 불명 상태로 두지 않기 위함
// (2026-09-03 사용자 지적: "이 기사에 들어간 사진은 출처가 없어").
export function imageCreditLabel(imageUrl, imageCredit) {
  if (imageCredit) return imageCredit;
  const url = String(imageUrl || '');
  if (/pixabay|r2\.dev/.test(url)) return '이미지 출처: Pixabay';
  if (/wikimedia\.org/.test(url)) return '이미지 출처: Wikimedia Commons';
  return '';
}
