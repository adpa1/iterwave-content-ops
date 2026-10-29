import { useEffect, useRef, useState } from "react";
import { api, ActionItem, ContentItem, Draft, Goal, HotTopicItem, SpiralRound } from "../api";
import { NAV_ITEMS } from "../Sidebar";

interface SearchHit {
  id: string;
  title: string;
  detail: string;
  page: string;
  tab?: string;
  roundId?: number;
  contentId?: string;
  kind: "页面" | "目标" | "热点" | "任务" | "轮次" | "草稿" | "内容";
}

interface Props {
  onNavigate: (page: string, tab?: string, focus?: { roundId?: number; contentId?: string }) => void;
}

const norm = (value: unknown) => String(value ?? "").trim().toLocaleLowerCase();

export default function GlobalSearch({ onNavigate }: Props) {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [partialError, setPartialError] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const [mobileOpen, setMobileOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const onPointerDown = (event: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setOpen(false);
        setMobileOpen(false);
      }
    };
    const onShortcut = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const typing = target && (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));
      if (event.key === "/" && !typing && !event.metaKey && !event.ctrlKey && !event.altKey) {
        event.preventDefault();
        setMobileOpen(true);
        setOpen(true);
        inputRef.current?.focus();
      }
    };
    document.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("keydown", onShortcut);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("keydown", onShortcut);
    };
  }, []);

  useEffect(() => {
    const q = norm(query);
    if (q.length < 2) {
      setHits([]);
      setPartialError(false);
      setBusy(false);
      setActiveIndex(0);
      return;
    }

    let cancelled = false;
    const timer = window.setTimeout(async () => {
      setBusy(true);
      setPartialError(false);

      const [goalsRes, hotRes, actionsRes, roundsRes, draftsRes] = await Promise.allSettled([
        api.goals(),
        api.hotTopics(),
        api.actionItems(),
        api.rounds(),
        api.drafts(),
      ]);

      if (cancelled) return;
      const hitsNext: SearchHit[] = [];
      const failedSources = [goalsRes, hotRes, actionsRes, roundsRes, draftsRes]
        .filter((result) => result.status === "rejected").length;

      for (const item of NAV_ITEMS) {
        if (norm(item.label).includes(q)) {
          hitsNext.push({
            id: `page-${item.page}`,
            title: item.label,
            detail: "打开功能页面",
            page: item.page,
            kind: "页面",
          });
        }
      }

      const has = (title: unknown, detail = "") => norm(`${String(title ?? "")} ${detail}`).includes(q);
      if (goalsRes.status === "fulfilled") {
        for (const goal of goalsRes.value as Goal[]) {
          if (has(goal.goal_text, goal.target_metric)) {
            hitsNext.push({
              id: `goal-${goal.id}`,
              title: goal.goal_text,
              detail: `目标 · ${goal.status === "active" ? "进行中" : goal.status}`,
              page: "goals",
              kind: "目标",
            });
          }
        }
      }
      if (hotRes.status === "fulfilled") {
        for (const topic of hotRes.value.items as HotTopicItem[]) {
          if (has(topic.title, topic.source)) {
            hitsNext.push({
              id: `hot-${topic.id}`,
              title: topic.title,
              detail: `热点 · ${topic.source}`,
              page: "hot",
              kind: "热点",
            });
          }
        }
      }
      if (actionsRes.status === "fulfilled") {
        for (const action of actionsRes.value.items as ActionItem[]) {
          if (has(action.title, action.detail ?? "")) {
            hitsNext.push({
              id: `action-${action.id}`,
              title: action.title,
              detail: `待处理任务 · ${action.detail ?? "需要处理"}`,
              page: action.type === "goal_strategy" ? "goals" : action.type === "publish_decision" ? "production" : "operations",
              tab: action.type === "publish_decision" ? "review" : undefined,
              kind: "任务",
            });
          }
        }
      }

      const rounds = roundsRes.status === "fulfilled" ? roundsRes.value as SpiralRound[] : [];
      if (roundsRes.status === "fulfilled") {
        for (const round of rounds) {
          const title = `第 ${round.round_number} 轮`;
          if (has(title, round.status)) {
            hitsNext.push({
              id: `round-${round.id}`,
              title,
              detail: `内容轮次 · ${round.status}`,
              page: "production",
              tab: "rounds",
              roundId: round.id,
              kind: "轮次",
            });
          }
        }
      }
      if (draftsRes.status === "fulfilled") {
        for (const draft of draftsRes.value as Draft[]) {
          if (has(draft.title, draft.content_id)) {
            hitsNext.push({
              id: `draft-${draft.id}`,
              title: draft.title || draft.content_id,
              detail: `草稿 · ${draft.platform} · ${draft.status}`,
              page: "production",
              tab: "review",
              roundId: draft.round_id ?? undefined,
              contentId: draft.content_id,
              kind: "草稿",
            });
          }
        }
      }

      // 搜索最近三轮的具体内容，避免对整个历史轮次发起无上限的请求。
      if (rounds.length) {
        const contentResults = await Promise.allSettled(
          rounds.slice(0, 3).map((round) => api.contents(round.id)),
        );
        if (cancelled) return;
        if (contentResults.some((result) => result.status === "rejected")) {
          // 不阻断其他来源的搜索结果；只提示结果可能不完整。
          setPartialError(true);
        }
        for (const result of contentResults) {
          if (result.status !== "fulfilled") continue;
          for (const item of result.value.items as ContentItem[]) {
            if (has(item.title, item.body)) {
              hitsNext.push({
                id: `content-${item.id}`,
                title: item.title || "未命名内容",
                detail: `内容 · ${item.platform} · 第 ${result.value.round_id} 轮`,
                page: "production",
                tab: "review",
                roundId: item.round_id ?? result.value.round_id,
                contentId: item.id,
                kind: "内容",
              });
            }
          }
        }
      }

      // 记录去重：同一页面/标题只保留一个结果，优先保留更具体的业务记录。
      const seen = new Set<string>();
      const unique = hitsNext.filter((hit) => {
        const key = `${hit.page}|${norm(hit.title)}`;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      });
      setHits(unique.slice(0, 12));
      setPartialError((prev) => prev || failedSources > 0);
      setActiveIndex(0);
      setBusy(false);
    }, 300);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [query]);

  const choose = (hit: SearchHit) => {
    setOpen(false);
    setMobileOpen(false);
    setQuery("");
    onNavigate(hit.page, hit.tab, { roundId: hit.roundId, contentId: hit.contentId });
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Escape") {
      setOpen(false);
      inputRef.current?.blur();
    } else if (event.key === "ArrowDown" && hits.length) {
      event.preventDefault();
      setOpen(true);
      setActiveIndex((index) => (index + 1) % hits.length);
    } else if (event.key === "ArrowUp" && hits.length) {
      event.preventDefault();
      setOpen(true);
      setActiveIndex((index) => (index - 1 + hits.length) % hits.length);
    } else if (event.key === "Enter" && open && hits[activeIndex]) {
      event.preventDefault();
      choose(hits[activeIndex]);
    }
  };

  return (
    <div className={mobileOpen ? "global-search mobile-open" : "global-search"} ref={rootRef}>
      <button
        type="button"
        className="mobile-search-toggle"
        aria-label="打开全局搜索"
        aria-expanded={mobileOpen}
        onClick={() => {
          setMobileOpen((value) => !value);
          setOpen((value) => !value);
          window.setTimeout(() => inputRef.current?.focus(), 0);
        }}
      >⌕</button>
      <label className="sr-only" htmlFor="globalSearch">全局搜索</label>
      <input
        ref={inputRef}
        id="globalSearch"
        type="search"
        className={mobileOpen ? "search mobile-open" : "search"}
        role="combobox"
        aria-autocomplete="list"
        aria-expanded={open && norm(query).length >= 2}
        aria-controls="global-search-results"
        aria-label="搜索页面、目标、热点、任务、草稿和内容"
        placeholder="搜索页面、目标、热点、任务或内容…"
        value={query}
        onChange={(event) => { setQuery(event.target.value); setOpen(true); }}
        onFocus={() => setOpen(true)}
        onKeyDown={onKeyDown}
      />
      {open && norm(query).length < 2 && (
        <div className="search-popover search-hint" role="status">
          输入至少 2 个字，可搜索页面、目标、热点、任务、草稿和最近三轮内容。
        </div>
      )}
      {open && norm(query).length >= 2 && (
        <div className="search-popover" id="global-search-results" role="listbox" aria-label="搜索结果">
          {busy && <div className="search-state">正在搜索…</div>}
          {!busy && partialError && <div className="search-partial">部分数据源暂不可用，以下结果可能不完整。</div>}
          {!busy && hits.length === 0 && <div className="search-state">没有找到匹配项。可尝试更短的关键词。</div>}
          {!busy && hits.map((hit, index) => (
            <button
              type="button"
              role="option"
              aria-selected={activeIndex === index}
              className={`search-result ${activeIndex === index ? "active" : ""}`}
              key={hit.id}
              onMouseEnter={() => setActiveIndex(index)}
              onClick={() => choose(hit)}
            >
              <span className="search-kind">{hit.kind}</span>
              <span className="search-result-copy">
                <strong>{hit.title}</strong>
                <small>{hit.detail}</small>
              </span>
              <span className="search-enter" aria-hidden="true">↵</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
