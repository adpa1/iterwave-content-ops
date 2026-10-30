/**
 * 单文件原型演示用的「假后端」。
 *
 * 打包时把本地后端真实响应快照成 fixtures.json（见 src/demo/fixtures.json），
 * 运行时接管 window.fetch，直接返回快照数据，因此这个 HTML 不需要任何后端、
 * 也不发任何真实网络请求即可浏览全部页面。
 */
import fixtures from "./fixtures.json";

type Json = unknown;
interface Fixtures {
  routes: Record<string, Json>;
  posts: Record<string, Json>;
  roundDetails: Record<string, Json>;
  pipelines: Record<string, Json>;
  contentsByRound: Record<string, Json>;
}

const F = fixtures as unknown as Fixtures;

function ok(data: Json, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

/** 变更类接口的兜底返回：原型里不落库，给一个「看起来成功」的结果即可 */
function mutate(apiPath: string): Json {
  if (F.posts[apiPath]) return F.posts[apiPath];
  if (apiPath === "/api/notify/test") {
    return { results: { macos: "ok", wecom: "disabled", feishu: "disabled" } };
  }
  if (apiPath === "/api/ops/pause") return { paused: true };
  if (apiPath === "/api/orchestration/validate") return { valid: true, errors: [] };
  if (apiPath === "/api/ops/pause") return { paused: true };
  if (apiPath.startsWith("/api/auth/")) return { ok: true };
  return { ok: true };
}

function readBody(init: RequestInit): Record<string, unknown> {
  try {
    return init.body ? JSON.parse(String(init.body)) : {};
  } catch {
    return {};
  }
}

function resolveGet(apiPath: string, search: URLSearchParams): Json | null {
  if (F.routes[apiPath] !== undefined) return F.routes[apiPath];

  const roundDetail = /^\/api\/rounds\/(\d+)$/.exec(apiPath);
  if (roundDetail) return F.roundDetails[roundDetail[1]] ?? F.roundDetails["5"] ?? {};

  const pipeline = /^\/api\/rounds\/(\d+)\/pipeline$/.exec(apiPath);
  if (pipeline) return F.pipelines[pipeline[1]] ?? F.pipelines["5"] ?? {};

  if (apiPath === "/api/contents") {
    const rid = search.get("round_id") ?? "5";
    return F.contentsByRound[rid] ?? { round_id: Number(rid), items: [] };
  }
  return null;
}

export function installDemoApi(): void {
  const w = window as unknown as { __iwDemoApi?: boolean };
  if (w.__iwDemoApi) return;
  w.__iwDemoApi = true;

  const originalFetch = window.fetch ? window.fetch.bind(window) : null;

  window.fetch = async (input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> => {
    const rawUrl =
      typeof input === "string" ? input : input instanceof URL ? input.href : (input as Request).url;
    let parsed: URL;
    try {
      parsed = new URL(rawUrl, window.location.href);
    } catch {
      return ok({ ok: true });
    }
    const pathname = parsed.pathname.replace(/\/+$/, "");
    const idx = pathname.indexOf("/api/");
    if (idx < 0) {
      if (originalFetch) return originalFetch(input, init);
      return ok({});
    }
    const apiPath = pathname.slice(idx);
    const method = (init.method || (input instanceof Request ? input.method : "GET")).toUpperCase();

    if (apiPath === "/api/auth/status") return ok({ required: true, authed: true });
    if (apiPath.startsWith("/api/auth/")) return ok({ ok: true });

    if (method === "GET") {
      const hit = resolveGet(apiPath, parsed.searchParams);
      return ok(hit ?? {});
    }

    if (apiPath === "/api/ops/pause") return ok({ paused: Boolean(readBody(init).paused) });
    return ok(mutate(apiPath));
  };

  // 卡片图占位：演示包里没有图片文件，加载失败时替换为内联占位图
  const placeholder =
    "data:image/svg+xml;utf8," +
    encodeURIComponent(
      '<svg xmlns="http://www.w3.org/2000/svg" width="720" height="960">' +
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">' +
        '<stop offset="0" stop-color="#f3e9dc"/><stop offset="1" stop-color="#e8c9a0"/></linearGradient></defs>' +
        '<rect width="720" height="960" fill="url(#g)"/>' +
        '<text x="360" y="470" text-anchor="middle" font-size="34" fill="#8a6f52" font-family="sans-serif">卡片图（演示占位）</text>' +
        '<text x="360" y="520" text-anchor="middle" font-size="20" fill="#a8907a" font-family="sans-serif">静态原型不含图片文件</text>' +
        "</svg>",
    );

  window.addEventListener(
    "error",
    (e) => {
      const t = e.target as HTMLImageElement | null;
      if (t && t.tagName === "IMG" && !t.dataset.demoFallback) {
        t.dataset.demoFallback = "1";
        t.src = placeholder;
      }
    },
    true,
  );

  // 演示水印（R20：明确标注这是静态原型）
  window.addEventListener("DOMContentLoaded", () => {
    const tip = document.createElement("div");
    tip.textContent = "静态原型演示 · 页面为真实前端组件的离线快照，不连接后端、不执行真实操作";
    tip.style.cssText = [
      "position:fixed", "left:14px", "bottom:12px", "z-index:999",
      "padding:6px 12px", "border-radius:999px",
      "background:rgba(19,33,59,.86)", "color:#fff",
      "font-size:11px", "line-height:1.4", "letter-spacing:.2px",
      "box-shadow:0 6px 18px rgba(16,32,59,.28)", "pointer-events:none", "max-width:72vw",
    ].join(";");
    document.body.appendChild(tip);
  });
}
