/**
 * Cloudflare Worker — 이미지 R2 업로드 API
 * 배포: wrangler deploy
 * 
 * POST /upload
 * Body: FormData { file: File } or JSON { base64: string, mimeType: string, filename: string }
 */

export default {
  async fetch(request, env) {
    // CORS
    if (request.method === 'OPTIONS') {
      return new Response(null, {
        headers: {
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Methods': 'POST, OPTIONS',
          'Access-Control-Allow-Headers': 'Content-Type, Authorization',
        }
      });
    }

    if (request.method !== 'POST') {
      return new Response('Method not allowed', { status: 405 });
    }

    // 인증 체크
    const auth = request.headers.get('Authorization');
    if (auth !== `Bearer ${env.UPLOAD_SECRET}`) {
      return new Response('Unauthorized', { status: 401 });
    }

    try {
      const contentType = request.headers.get('Content-Type') || '';
      let fileData, fileName, mimeType;

      if (contentType.includes('application/json')) {
        // base64 이미지 (AI 생성)
        const body = await request.json();
        const base64Data = body.base64.replace(/^data:[^;]+;base64,/, '');
        fileData = Uint8Array.from(atob(base64Data), c => c.charCodeAt(0));
        mimeType = body.mimeType || 'image/png';
        fileName = body.filename || `ai_${Date.now()}.png`;
      } else {
        // 파일 업로드
        const formData = await request.formData();
        const file = formData.get('file');
        fileData = await file.arrayBuffer();
        mimeType = file.type;
        fileName = `upload_${Date.now()}_${file.name.replace(/[^a-zA-Z0-9.]/g, '_')}`;
      }

      // R2에 업로드
      const key = `articles/${fileName}`;
      await env.R2_BUCKET.put(key, fileData, {
        httpMetadata: { contentType: mimeType }
      });

      const publicUrl = `${env.R2_PUBLIC_URL}/${key}`;

      return new Response(JSON.stringify({ url: publicUrl }), {
        headers: {
          'Content-Type': 'application/json',
          'Access-Control-Allow-Origin': '*',
        }
      });
    } catch (e) {
      return new Response(JSON.stringify({ error: e.message }), {
        status: 500,
        headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' }
      });
    }
  }
};
