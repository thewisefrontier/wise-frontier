export function onRequest(context) {
  const url = new URL(context.request.url);
  const id = url.searchParams.get("id") || "(none)";
  return new Response(
    `hello from pages function | route=/hello | id=${id} | ${new Date().toISOString()}`,
    { headers: { "content-type": "text/plain; charset=utf-8" } }
  );
}
