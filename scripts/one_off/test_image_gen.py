"""
test_image_gen.py
------------------
Gemini 이미지 생성 API(gemini-2.5-flash-image) 실제 호출 테스트.
GEMINI_API_KEY_4로 호출해서 성공/실패와 정확한 에러 메시지를 확인하기 위한 진단 스크립트.

실행: python scripts/one_off/test_image_gen.py
"""
import os
import json
import base64
import requests

GEMINI_MODEL = "gemini-2.5-flash-image"

def test():
    api_key = os.getenv("GEMINI_API_KEY_4") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("❌ GEMINI_API_KEY_4 (또는 GEMINI_API_KEY) 환경변수가 없습니다.")
        return

    print(f"[테스트] 모델: {GEMINI_MODEL}")
    print(f"[테스트] 키 앞 6자리: {api_key[:6]}... (총 {len(api_key)}자)")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}"
    payload = {
        "contents": [{
            "parts": [
                {"text": "A simple test image: a red circle on a white background"}
            ]
        }]
    }

    print(f"[테스트] 요청 URL: https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent")
    print(f"[테스트] 요청 본문: {json.dumps(payload, ensure_ascii=False)}")
    print("[테스트] 호출 중...")

    try:
        res = requests.post(url, json=payload, timeout=60)
        print(f"\n[결과] HTTP 상태코드: {res.status_code}")

        if res.status_code == 200:
            data = res.json()
            # 응답 구조 확인
            candidates = data.get("candidates", [])
            if not candidates:
                print("⚠️ 200 OK이지만 candidates가 비어있음")
                print(json.dumps(data, ensure_ascii=False, indent=2)[:1000])
                return

            parts = candidates[0].get("content", {}).get("parts", [])
            found_image = False
            for part in parts:
                if "inlineData" in part or "inline_data" in part:
                    inline = part.get("inlineData") or part.get("inline_data")
                    mime = inline.get("mimeType") or inline.get("mime_type")
                    img_data = inline.get("data", "")
                    img_bytes = base64.b64decode(img_data)
                    print(f"✅ 이미지 생성 성공! MIME: {mime}, 크기: {len(img_bytes)} bytes")
                    found_image = True
                elif "text" in part:
                    print(f"[텍스트 응답 포함]: {part['text'][:200]}")

            if not found_image:
                print("⚠️ 200 OK이지만 이미지 데이터를 못 찾음. 전체 응답 구조:")
                print(json.dumps(data, ensure_ascii=False, indent=2)[:1500])
        else:
            print(f"❌ 실패. 응답 본문:")
            print(res.text[:1500])

    except requests.exceptions.Timeout:
        print("❌ 타임아웃 (60초 초과)")
    except Exception as e:
        print(f"❌ 예외 발생: {type(e).__name__}: {e}")

if __name__ == "__main__":
    test()
