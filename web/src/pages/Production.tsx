import { Fragment, useEffect, useState } from "react";
import { useApp } from "../AppContext";
import { useUrlTab } from "../nav";
import {
  api, RunRoundReport, SpiralRound, Draft, Platform,
  PlanResponse, ContentItem, PipelineResponse, RecheckResult, Goal,
} from "../api";
import DemoBadge from "../components/DemoBadge";
import EmptyHint from "../components/EmptyHint";

const ROUND_STATUSES = ["created", "producing", "awaiting_publish", "collecting_metrics", "analyzing", "completed", "partial_completed", "failed", "cancelled"];
const ROUND_TARGETABLE: Record<string, string[]> = {
  created: ["producing", "cancelled"],
  producing: ["awaiting_publish", "failed", "cancelled"],
  awaiting_publish: ["collecting_metrics", "cancelled"],
  collecting_metrics: ["analyzing", "cancelled"],
  analyzing: ["completed", "partial_completed", "failed"],
};

const STATUS_CLS: Record<string, string> = {
  created: "gray", producing: "orange", awaiting_publish: "blue",
  collecting_metrics: "purple", analyzing: "teal",
  completed: "green", partial_completed: "orange", failed: "red", cancelled: "gray",
  draft_pending: "gray", draft_saved: "blue", user_published: "purple",
  data_fetching: "orange", published: "green", expired: "gray",
};

const GUARD_CLS: Record<string, string> = {
  pass: "green", warn: "orange", fail: "red", error: "red",
};

const PLATFORM_NAME: Record<Platform, string> = {
  xhs: "小红书", douyin: "抖音",
  kuaishou: "快手", wechat_channels: "视频号",
};

const ALL_PLATFORMS: Platform[] = ["xhs", "douyin", "kuaishou", "wechat_channels"];

const TABS = [
  { key: "plan", label: "总计划" },
  { key: "platforms", label: "平台分项计划" },
  { key: "pipeline", label: "生产线" },
  { key: "review", label: "内容编辑与审核" },
  { key: "rounds", label: "轮次状态" },
] as const;

type TabKey = typeof TABS[number]["key"];

const ROUND_STATUS_LABEL: Record<string, string> = {
  created: "已创建", producing: "生产中", awaiting_publish: "待发布", collecting_metrics: "采集中", analyzing: "复盘中", completed: "已完成", partial_completed: "部分完成", failed: "失败", cancelled: "已取消", paused: "已暂停",
};

const AUTHOR_LABEL: Record<string, string> = {
  "web-ui": "手动保存", round_create: "新建轮次", round_executor: "轮次执行", autogrow: "自动闭环", agent: "Agent",
};

interface AllocRow { dim: string; count: number }

export default function Production() {
  const { showToast, withPending, isPending } = useApp();

  const [tab, setTab] = useUrlTab<TabKey>(TABS.map((t) => t.key), "plan");

  const [rounds, setRounds] = useState<SpiralRound[]>([]);
  // 全局搜索可传入目标轮次，使跨页面跳转后选中正确轮次，而不是永远回到最新轮。
  const [currentRoundId, setCurrentRoundId] = useState<number | null>(() => {
    const raw = new URLSearchParams(window.location.search).get("focusRound");
    const parsed = raw ? Number(raw) : NaN;
    return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
  });
  const [roundDetail, setRoundDetail] = useState<(SpiralRound & { drafts: Draft[] }) | null>(null);

  // 总计划
  const [plan, setPlan] = useState<PlanResponse | null>(null);
  const [planTopicCount, setPlanTopicCount] = useState(5);
  const [planPlatforms, setPlanPlatforms] = useState<Platform[]>(ALL_PLATFORMS);
  const [allocRows, setAllocRows] = useState<AllocRow[]>([]);
  const [platformPlans, setPlatformPlans] = useState<Record<string, { topic_count: string; slots: string }>>({});
  const [cadence, setCadence] = useState<{ interval: string; rounds: string; goal_id: string }>({ interval: "", rounds: "", goal_id: "" });
  const [goals, setGoals] = useState<Goal[]>([]);
  const [suggestNotes, setSuggestNotes] = useState<string[]>([]);
  const [usePlan, setUsePlan] = useState(true);

  // 生产线
  const [pipeline, setPipeline] = useState<PipelineResponse | null>(null);

  // 内容编辑与审核
  const [contents, setContents] = useState<ContentItem[]>([]);
  const [reviewSearch, setReviewSearch] = useState("");
  const [reviewPlatform, setReviewPlatform] = useState<"all" | Platform>("all");
  const [reviewState, setReviewState] = useState("all");
  // 搜索结果可带目标内容 ID；加载轮次内容后自动选中并填入编辑区。
  const [selectedContentId, setSelectedContentId] = useState<string | null>(() => {
    const value = new URLSearchParams(window.location.search).get("focusContent");
    return value || null;
  });
  const [editTitle, setEditTitle] = useState("");
  const [editBody, setEditBody] = useState("");
  const [editTags, setEditTags] = useState("");
  const [recheck, setRecheck] = useState<RecheckResult | null>(null);
  const [rejectReason, setRejectReason] = useState("");
  /** 卡片图加载失败的序号（避免破图静默留白，明确提示文件缺失） */
  const [brokenCards, setBrokenCards] = useState<Record<number, true>>({});
  /** 当前弹层预览的卡片序号（null = 不展示预览层） */
  const [previewCard, setPreviewCard] = useState<number | null>(null);

  // 运行本轮
  const [topicCount, setTopicCount] = useState(5);
  const [selPlatforms, setSelPlatforms] = useState<Platform[]>(ALL_PLATFORMS);
  const [saveDrafts, setSaveDrafts] = useState(true);
  const [running, setRunning] = useState(false);
  const [report, setReport] = useState<RunRoundReport | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [noLogin, setNoLogin] = useState<Platform[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);

  // ---------------------------------------------------------------- 数据加载
  const refresh = async () => {
    try {
      const r = await api.rounds();
      setRounds(r);
      setLoadError(null);
      const targetId = currentRoundId ?? (r.length > 0 ? r[0].id : null);
      if (targetId) {
        setCurrentRoundId(targetId);
        try { setRoundDetail(await api.round(targetId)); } catch { /* ignore */ }
      }
    } catch (e: any) {
      // G-12：首屏加载失败必须可见 + 可重试，不能只弹一个 toast
      setLoadError(e?.message ?? "加载轮次失败");
    }
  };

  const loadPlan = async () => {
    try {
      const p = await api.plans();
      setPlan(p);
      setPlanTopicCount(p.plan.topic_count_per_platform);
      setPlanPlatforms((p.plan.enabled_platforms as Platform[]) ?? ALL_PLATFORMS);
      setAllocRows(Object.entries(p.plan.dimension_allocation ?? {}).map(([dim, count]) => ({ dim, count })));
      const pp: Record<string, { topic_count: string; slots: string }> = {};
      for (const pl of ALL_PLATFORMS) {
        const conf = p.plan.per_platform?.[pl];
        pp[pl] = { topic_count: conf?.topic_count ? String(conf.topic_count) : "", slots: (conf?.publish_slots ?? []).join(" ") };
      }
      setPlatformPlans(pp);
      setCadence({
        interval: p.plan.cadence?.round_interval_days != null ? String(p.plan.cadence.round_interval_days) : "",
        rounds: p.plan.cadence?.rounds_planned != null ? String(p.plan.cadence.rounds_planned) : "",
        goal_id: p.plan.cadence?.goal_id ?? "",
      });
    } catch (e: any) {
      setLoadError(`加载母计划失败：${e.message}`);
    }
  };

  const loadRoundData = async (roundId: number) => {
    try { setRoundDetail(await api.round(roundId)); } catch { /* ignore */ }
    try { setPipeline(await api.pipeline(roundId)); } catch { setPipeline(null); }
    try {
      const res = await api.contents(roundId);
      setContents(res.items);
      setSelectedContentId((prev) => {
        if (prev && res.items.some((i) => i.id === prev)) return prev;
        return res.items[0]?.id ?? null;
      });
    } catch { setContents([]); }
  };

  useEffect(() => {
    loadPlan();
    api.goals().then((g) => setGoals(g.filter((x) => x.status === "active"))).catch(() => {});
    refresh().catch((e) => showToast(`加载轮次失败: ${e.message}`, "error"));
    api.status()
      .then((s) => {
        if (s.enabled_platforms?.length) setSelPlatforms(s.enabled_platforms);
        setNoLogin((s.platforms_without_login ?? []) as Platform[]);
      })
      .catch(() => { /* 用默认四平台 */ });
  }, []);

  useEffect(() => {
    if (currentRoundId) loadRoundData(currentRoundId);
  }, [currentRoundId]);

  // 全局搜索可能在当前已打开的「内容生产」页内切换目标；
  // 组件不会因 page key 变化而重挂载，因此需要响应 URL 深链变化。
  useEffect(() => {
    const syncFocusFromUrl = () => {
      const params = new URLSearchParams(window.location.search);
      const rawRoundId = params.get("focusRound");
      const parsedRoundId = rawRoundId ? Number(rawRoundId) : NaN;
      if (Number.isInteger(parsedRoundId) && parsedRoundId > 0) {
        setCurrentRoundId(parsedRoundId);
      }
      const contentId = params.get("focusContent");
      if (contentId) setSelectedContentId(contentId);
    };
    window.addEventListener("popstate", syncFocusFromUrl);
    return () => window.removeEventListener("popstate", syncFocusFromUrl);
  }, []);

  // 选中内容 → 填充编辑器
  useEffect(() => {
    setBrokenCards({});
    setPreviewCard(null);
    const item = contents.find((c) => c.id === selectedContentId);
    if (!item) { setEditTitle(""); setEditBody(""); setEditTags(""); return; }
    setEditTitle(item.title);
    setEditBody(item.body);
    setEditTags(item.tags.join(" "));
    setRecheck(null);
  }, [selectedContentId, contents]);

  // 卡片大图预览层：Esc 关闭
  useEffect(() => {
    if (previewCard === null) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setPreviewCard(null); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [previewCard]);

  // ---------------------------------------------------------------- 操作
  const handleCreateRound = async () => {
    try {
      const res = await api.createRound({ topic_count: topicCount });
      showToast(`✓ 第 ${res.round_number} 轮已创建`);
      setReport(null);
      await refresh();
      setCurrentRoundId(res.id);
      setTab("pipeline");
    } catch (e: any) { showToast(`创建失败: ${e.message}`, "error"); }
  };

  const handleRun = async () => {
    if (!currentRoundId || running) return;
    setRunning(true);
    setRunError(null);
    setReport(null);
    try {
      const res = await api.runRound(
        currentRoundId,
        usePlan ? {} : { topic_count: topicCount, platforms: selPlatforms, save_drafts: saveDrafts },
      );
      setReport(res);
      const isDryRun = res.steps.some((s) => s.step === "dry_run");
      if (res.final_status === "failed") {
        showToast(`✕ 本轮执行失败：${res.errors.join("；") || "未知原因"}`, "error");
      } else if (isDryRun) {
        showToast(`✓ 已产出 ${res.contents_created} 篇内容（本次未执行存草稿）`, "success");
      } else {
        showToast(`✓ 本轮完成：内容 ${res.contents_created} / 草稿 ${res.drafts_saved}`, "success");
      }
      await refresh();
      await loadRoundData(currentRoundId);
    } catch (e: any) {
      setRunError(e.message);
      showToast(`运行失败: ${e.message}`, "error");
    } finally {
      setRunning(false);
    }
  };

  const handleTransition = async (target: string) => {
    if (!currentRoundId) return;
    try {
      await api.transitionRound(currentRoundId, target, "Web UI 触发");
      showToast(`↻ 状态已变更为 ${target}`);
      await refresh();
      await loadRoundData(currentRoundId);
    } catch (e: any) { showToast(`变更失败: ${e.message}`, "error"); }
  };

  const confirmPublish = async (draftId: string) => {
    try {
      await api.confirmPublish(draftId);
      showToast("✓ 已确认发布");
      await refresh();
      if (currentRoundId) await loadRoundData(currentRoundId);
    } catch (e: any) { showToast(`确认失败: ${e.message}`, "error"); }
  };

  /** 「✕ 不发了」：draft_pending / draft_saved → cancelled（R9：不冻结未发布草稿） */
  const cancelDraft = async (draftId: string) => {
    try {
      await api.cancelDraft(draftId);
      showToast("已取消该草稿");
      await refresh();
      if (currentRoundId) await loadRoundData(currentRoundId);
    } catch (e: any) { showToast(`取消失败: ${e.message}`, "error"); }
  };

  const handleSavePlan = async () => {
    const alloc: Record<string, number> = {};
    for (const r of allocRows) {
      const dim = r.dim.trim();
      if (!dim) continue;
      alloc[dim] = Math.max(0, Number(r.count) || 0);
    }
    await withPending("save-plan", async () => {
      try {
        const allocTotal = Object.values(alloc).reduce((a, b) => a + b, 0);
        if (allocTotal > planTopicCount) {
          showToast(`维度分配合计 ${allocTotal} 条，超过每平台选题数 ${planTopicCount}`, "error");
          return;
        }
        const per_platform: Record<string, { topic_count?: number; publish_slots?: string[] }> = {};
        for (const pl of planPlatforms) {
          const v = platformPlans[pl];
          if (!v) continue;
          const slots = v.slots.split(/[\s,，]+/).map((x) => x.trim()).filter(Boolean);
          const bad = slots.find((x) => !/^([01]\d|2[0-3]):[0-5]\d$/.test(x));
          if (bad) { showToast(`${PLATFORM_NAME[pl]} 发布时段「${bad}」格式应为 HH:MM`, "error"); return; }
          per_platform[pl] = {
            ...(v.topic_count ? { topic_count: Math.max(1, Math.min(planTopicCount, Number(v.topic_count) || 1)) } : {}),
            ...(slots.length ? { publish_slots: slots } : {}),
          };
        }
        const res = await api.savePlan({
          topic_count_per_platform: planTopicCount,
          dimension_allocation: alloc,
          enabled_platforms: planPlatforms,
          content_format: plan?.plan.content_format ?? "image_text",
          per_platform,
          cadence: {
            ...(cadence.interval ? { round_interval_days: Number(cadence.interval) } : {}),
            ...(cadence.rounds ? { rounds_planned: Number(cadence.rounds) } : {}),
            ...(cadence.goal_id ? { goal_id: cadence.goal_id } : {}),
          },
        });
        if (res.changed) {
          showToast(
            `✓ 母计划已更新，${res.stale_rounds.length} 个活跃轮次已标记失效`,
            "success",
          );
        } else {
          showToast(`计划内容没有变化`, "info");
        }
        await loadPlan();
        await refresh();
      } catch (e: any) {
        showToast(`保存母计划失败: ${e.message}`, "error");
      }
    });
  };

  const handleSaveContent = async () => {
    if (!selectedContentId) return;
    const tags = editTags.split(/[,，\s]+/).map((t) => t.trim().replace(/^#/, "")).filter(Boolean);
    await withPending("save-content", async () => {
      try {
        const res = await api.updateContent(selectedContentId, {
          title: editTitle, body: editBody, tags, reason: "UI 编辑",
        });
        showToast(res.changed ? "✓ 文案已保存（user_override 已留痕）" : "内容无变化", "success");
        await loadRoundData(currentRoundId!);
      } catch (e: any) {
        showToast(`保存失败: ${e.message}`, "error");
      }
    });
  };

  /** G-36：小红书草稿只在浏览器本地，关闭即消失 → 提供「复制文案」闭环 */
  const handleCopy = async () => {
    if (!selectedContentId) return;
    const tags = editTags
      .split(/[,，\s]+/)
      .map((t) => t.trim().replace(/^#+/, ""))
      .filter(Boolean)
      .map((t) => `#${t}`);
    const text = [editTitle.trim(), editBody.trim(), tags.join(" ")]
      .filter(Boolean)
      .join("\n\n");
    try {
      await navigator.clipboard.writeText(text);
      showToast("✓ 已复制标题 + 正文 + 标签，可直接粘贴到平台", "success");
    } catch {
      showToast("复制失败，请手动选中文本框复制", "error");
    }
  };

  /** 按当前标题/正文重新出图：封面 + 正文分页（历史内容也能补图） */
  const handleRegenerateCards = async () => {
    if (!selectedContentId) return;
    await withPending("regen-cards", async () => {
      try {
        const res = await api.regenerateContentCards(selectedContentId);
        showToast(`✓ 已生成 ${res.image_count} 张卡片`, "success");
        await loadRoundData(currentRoundId!);
      } catch (e: any) {
        showToast(`生成卡片失败: ${e.message}`, "error");
      }
    });
  };

  const handleRecheck = async () => {
    if (!selectedContentId) return;
    await withPending("recheck-content", async () => {
      try {
        const res = await api.recheckContent(selectedContentId);
        setRecheck(res);
        showToast(
          res.passes ? `✓ 目标检查通过（${res.result}）` : `✕ 目标检查未通过：${res.reason}`,
          res.passes ? "success" : "error",
        );
        await loadRoundData(currentRoundId!);
      } catch (e: any) {
        showToast(`重检失败: ${e.message}`, "error");
      }
    });
  };

  const handleReject = async () => {
    if (!selectedContentId) return;
    if (!rejectReason.trim()) { showToast("请先填写驳回理由", "error"); return; }
    await withPending("reject-content", async () => {
      try {
        const res = await api.rejectContent(selectedContentId, rejectReason.trim());
        showToast(`✕ 已驳回，影响草稿 ${res.affected_drafts} 条`, "success");
        setRejectReason("");
        await loadRoundData(currentRoundId!);
      } catch (e: any) {
        showToast(`驳回失败: ${e.message}`, "error");
      }
    });
  };

  const handleSuggest = async () => {
    if (!cadence.goal_id) { showToast("先选择要拆分的目标", "error"); return; }
    await withPending("plan-suggest", async () => {
      try {
        const r = await api.suggestPlanFromGoal(cadence.goal_id);
        setPlanTopicCount(r.suggestion.topic_count_per_platform);
        setPlanPlatforms(r.suggestion.enabled_platforms as Platform[]);
        setCadence((c) => ({
          ...c,
          interval: String(r.suggestion.cadence.round_interval_days ?? ""),
          rounds: String(r.suggestion.cadence.rounds_planned ?? ""),
        }));
        setSuggestNotes(r.notes);
        showToast("已按目标填好建议，确认后点「保存母计划」生效", "success");
      } catch (e: any) { showToast(`拆分失败：${e.message}`, "error"); }
    });
  };

  const handleDeleteContent = async () => {
    if (!selectedContentId) return;
    const item = contents.find((c) => c.id === selectedContentId);
    const extra = item?.draft_status === "draft_saved" ? "\n这篇已存到平台草稿箱，系统不会去平台删除，请自行清理。" : "";
    if (!window.confirm(`删除「${item?.title ?? ""}」？文案、草稿记录和卡片图片会一起删除，不可恢复。${extra}`)) return;
    await withPending("delete-content", async () => {
      try {
        const r = await api.deleteContents([selectedContentId]);
        if (r.blocked.length) { showToast(r.blocked[0].reason, "error"); return; }
        showToast("已删除这篇内容", "success");
        setSelectedContentId(null);
        if (currentRoundId) await loadRoundData(currentRoundId);
      } catch (e: any) { showToast(`删除失败：${e.message}`, "error"); }
    });
  };

  const handleDeleteRound = async () => {
    if (!roundDetail) return;
    if (!window.confirm(`删除第 ${roundDetail.round_number} 轮？本轮所有内容、草稿记录、卡片图片一起删除，不可恢复。`)) return;
    await withPending("delete-round", async () => {
      try {
        const r = await api.deleteRound(roundDetail.id);
        showToast(`已删除第 ${r.round_number} 轮（内容 ${r.contents_deleted} 篇${r.platform_drafts_left ? `，请到平台草稿箱清理 ${r.platform_drafts_left} 篇` : ""}）`, "success");
        setCurrentRoundId(null);
        setRoundDetail(null);
        await refresh();
      } catch (e: any) { showToast(`删除失败：${e.message}`, "error"); }
    });
  };

  const togglePlatform = (p: Platform) => {
    setSelPlatforms((prev) => prev.includes(p) ? prev.filter((x) => x !== p) : [...prev, p]);
  };

  const togglePlanPlatform = (p: Platform) => {
    setPlanPlatforms((prev) => prev.includes(p) ? prev.filter((x) => x !== p) : [...prev, p]);
  };

  const canRun = !!roundDetail && roundDetail.status === "created" && !running && (usePlan || selPlatforms.length > 0);
  const selectedContent = contents.find((c) => c.id === selectedContentId) ?? null;
  const filteredReviewContents = contents.filter((item) => {
    const query = reviewSearch.trim().toLowerCase();
    const matchesQuery = !query || [item.title, item.body, item.topic_id, ...item.tags].some((value) => String(value ?? "").toLowerCase().includes(query));
    const matchesPlatform = reviewPlatform === "all" || item.platform === reviewPlatform;
    const status = item.draft_status ?? "not_saved";
    const matchesState = reviewState === "all"
      || (reviewState === "needs_review" && (item.guard_outcome !== "pass" || item.review_rejected))
      || (reviewState === "not_saved" && status === "draft_pending")
      || (reviewState === "saved" && status === "draft_saved")
      || (reviewState === "rejected" && !!item.review_rejected);
    return matchesQuery && matchesPlatform && matchesState;
  });

  // ---------------------------------------------------------------- 渲染
  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">CONTENT OPERATIONS ROOM</div>
          <h1 className="page-title">内容生产</h1>
          <div className="page-desc">
            按轮次生产内容：选题 → 文案 → 质检 → 存草稿 → 你确认发布 → 复盘。所有草稿只能存草稿，系统永不自动点发布按钮。
          </div>
        </div>
        <div className="head-actions">
          <button className="btn primary" onClick={handleCreateRound}>＋ 创建新一轮</button>
          <button className="btn" onClick={() => { refresh(); loadPlan(); if (currentRoundId) loadRoundData(currentRoundId); }}>↻ 刷新</button>
        </div>
      </div>

      <div className="tabs" style={{ marginBottom: 14 }}>
        {TABS.map((t) => (
          <button key={t.key} className={`tab ${tab === t.key ? "active" : ""}`} onClick={() => setTab(t.key)}>
            {t.label}
          </button>
        ))}
      </div>

      {loadError && (
        <div className="panel" style={{ borderColor: "var(--red)", marginBottom: 14 }}>
          <div className="panel-sub" style={{ color: "var(--red)", display: "flex", gap: 10, alignItems: "center" }}>
            <span>! 加载失败：{loadError}</span>
            <button className="btn sm" onClick={() => { refresh(); loadPlan(); }}>↻ 重试</button>
          </div>
        </div>
      )}

      {/* ============ ① 总计划 ============ */}
      {tab === "plan" && (
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">⌘</span>总计划（母计划）</div>
              <div className="panel-sub">
                修改总计划后，基于旧计划的轮次和草稿会被标记为过期
              </div>
            </div>
            <div style={{ fontFamily: "monospace", fontSize: 11, color: "var(--muted)" }}>
              版本 {plan?.signature ? plan.signature.slice(0, 8) : "—"}
              <span className={`tag ${plan?.source === "plan_settings" ? "green" : "gray"}`} style={{ marginLeft: 8 }}>
                {plan?.source === "plan_settings" ? "已保存" : plan?.source === "derived" ? "推导值" : "—"}
              </span>
            </div>
          </div>

          {!plan && <div className="empty">加载母计划中…</div>}

          {plan && (
            <>
              <div className="grid two">
                <div>
                  <div className="panel-title" style={{ fontSize: 13, marginBottom: 8 }}>每平台选题数</div>
                  <input
                    type="number" min={1} max={50} value={planTopicCount}
                    onChange={(e) => setPlanTopicCount(Math.max(1, Math.min(50, Number(e.target.value) || 1)))}
                    style={{ width: 90 }}
                  />
                  <div className="note" style={{ marginTop: 6 }}>
                    生产线默认按母计划执行；平台分项可单独设置更少的篇数。
                  </div>

                  <div className="panel-title" style={{ fontSize: 13, margin: "16px 0 8px" }}>启用平台</div>
                  <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                    {ALL_PLATFORMS.map((p) => (
                      <button
                        key={p}
                        className={`btn sm ${planPlatforms.includes(p) ? "primary" : ""}`}
                        onClick={() => togglePlanPlatform(p)}
                      >
                        {PLATFORM_NAME[p]}
                      </button>
                    ))}
                  </div>
                  {planPlatforms.length === 0 && (
                    <div className="note" style={{ color: "var(--red)", marginTop: 6 }}>至少启用一个平台</div>
                  )}
                </div>

                <div>
                  <div className="panel-title" style={{ fontSize: 13, marginBottom: 8 }}>维度分配</div>
                  {allocRows.length === 0 && (
                    <EmptyHint next="点下方「＋ 添加维度」录入维度名与条数（如「AI教育 3」），留空表示不限制">暂无维度分配</EmptyHint>
                  )}
                  <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                    {allocRows.map((row, i) => (
                      <div key={i} style={{ display: "flex", gap: 6 }}>
                        <input
                          value={row.dim} placeholder="维度名" style={{ flex: 1 }}
                          onChange={(e) => setAllocRows((prev) => prev.map((r, j) => j === i ? { ...r, dim: e.target.value } : r))}
                        />
                        <input
                          type="number" min={0} value={row.count} style={{ width: 72 }}
                          onChange={(e) => setAllocRows((prev) => prev.map((r, j) => j === i ? { ...r, count: Number(e.target.value) || 0 } : r))}
                        />
                        <button className="btn sm" onClick={() => setAllocRows((prev) => prev.filter((_, j) => j !== i))}>×</button>
                      </div>
                    ))}
                  </div>
                  <button className="btn sm" style={{ marginTop: 8 }} onClick={() => setAllocRows((prev) => [...prev, { dim: "", count: 1 }])}>
                    ＋ 添加维度
                  </button>
                </div>
              </div>

              <div className="grid two" style={{ marginTop: 16 }}>
                <div>
                  <div className="panel-title" style={{ fontSize: 13, marginBottom: 8 }}>各平台篇数与发布时段</div>
                  <div className="table-wrap">
                    <table>
                      <thead><tr><th>平台</th><th style={{ width: 90 }}>每轮篇数</th><th>发布时段（自动排期）</th></tr></thead>
                      <tbody>
                        {planPlatforms.map((pl) => (
                          <tr key={pl}>
                            <td>{PLATFORM_NAME[pl]}</td>
                            <td>
                              <input className="field" type="number" min={1} max={planTopicCount} style={{ width: 70 }}
                                placeholder={String(planTopicCount)} aria-label={`${PLATFORM_NAME[pl]} 每轮篇数`}
                                value={platformPlans[pl]?.topic_count ?? ""}
                                onChange={(e) => setPlatformPlans((prev) => ({ ...prev, [pl]: { ...(prev[pl] ?? { slots: "" }), topic_count: e.target.value } }))} />
                            </td>
                            <td>
                              <input className="field" style={{ width: "100%", minWidth: 0, maxWidth: 180 }} placeholder="如 12:30 20:00"
                                aria-label={`${PLATFORM_NAME[pl]} 发布时段`}
                                value={platformPlans[pl]?.slots ?? ""}
                                onChange={(e) => setPlatformPlans((prev) => ({ ...prev, [pl]: { ...(prev[pl] ?? { topic_count: "" }), slots: e.target.value } }))} />
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <div className="note" style={{ marginTop: 6 }}>篇数留空 = 跟随母计划；发布时段需在「Agent 编排」开启「自动排期」后生效。</div>
                </div>
                <div>
                  <div className="panel-title" style={{ fontSize: 13, marginBottom: 8 }}>节奏与目标拆分</div>
                  <div className="toolbar" style={{ marginBottom: 8 }}>
                    <label className="flex" style={{ gap: 6, alignItems: "center", fontSize: 12 }}>
                      关联目标
                      <select className="field" value={cadence.goal_id} onChange={(e) => setCadence({ ...cadence, goal_id: e.target.value })}>
                        <option value="">不关联</option>
                        {goals.map((g) => <option key={g.id} value={g.id}>{g.goal_text}</option>)}
                      </select>
                    </label>
                    <button className="btn sm" onClick={handleSuggest} disabled={!cadence.goal_id || isPending("plan-suggest")}>
                      {isPending("plan-suggest") ? "计算中…" : "按目标拆分"}
                    </button>
                  </div>
                  <div className="toolbar">
                    <label className="flex" style={{ gap: 6, alignItems: "center", fontSize: 12 }}>
                      每
                      <input className="field" type="number" min={0.5} max={90} step={0.5} style={{ width: 70 }}
                        value={cadence.interval} placeholder="—" onChange={(e) => setCadence({ ...cadence, interval: e.target.value })} />
                      天一轮
                    </label>
                    <label className="flex" style={{ gap: 6, alignItems: "center", fontSize: 12 }}>
                      本阶段共
                      <input className="field" type="number" min={1} max={200} style={{ width: 70 }}
                        value={cadence.rounds} placeholder="不限" onChange={(e) => setCadence({ ...cadence, rounds: e.target.value })} />
                      轮
                    </label>
                  </div>
                  {suggestNotes.length > 0 && (
                    <ul className="note" style={{ marginTop: 8, paddingLeft: 18 }}>
                      {suggestNotes.map((n, i) => <li key={i}>{n}</li>)}
                    </ul>
                  )}
                  <div className="note" style={{ marginTop: 6 }}>自动闭环按这里的间隔开下一轮，开满计划轮数后自动停止。</div>
                </div>
              </div>

              <div style={{ marginTop: 16, display: "flex", gap: 8, alignItems: "center" }}>
                <button
                  className="btn primary" onClick={handleSavePlan}
                  disabled={isPending("save-plan") || planPlatforms.length === 0}
                >
                  {isPending("save-plan") ? "提交中…" : "✓ 保存母计划"}
                </button>
                <span className="note">
                  {plan.updated_at ? `上次保存 ${plan.updated_at}` : "尚未在 UI 保存过（当前为推导值）"}
                </span>
              </div>

              {plan.stale_rounds.length > 0 && (
                <div className="note" style={{ marginTop: 12, color: "var(--orange)" }}>
                  ! 以下活跃轮次的签名与当前母计划不一致（下游已标记为基于旧计划）：
                  {plan.stale_rounds.map((r) => ` 第 ${r.round_number} 轮（${ROUND_STATUS_LABEL[r.status] ?? r.status}）`).join(" · ")}
                </div>
              )}

              <details style={{ marginTop: 18 }}>
                <summary className="panel-title" style={{ fontSize: 13, marginBottom: 8, cursor: "pointer" }}>计划变更记录（最近 10 条）</summary>
                <div className="table-wrap">
                  <table>
                    <thead><tr><th>版本</th><th>适用范围</th><th>来源</th><th>时间</th></tr></thead>
                    <tbody>
                      {plan.history.length === 0 && (
                        <tr><td colSpan={4}>
                          <EmptyHint className="" next="点上方「保存母计划」后这里会记录版本">暂无签名记录</EmptyHint>
                        </td></tr>
                      )}
                      {plan.history.map((h, i) => (
                        <tr key={i}>
                          <td style={{ fontFamily: "monospace", fontSize: 11 }}>{h.plan_signature.slice(0, 16)}…</td>
                          <td>{h.round_id != null ? `轮次 ${h.round_id}` : "母计划"}</td>
                          <td>{AUTHOR_LABEL[(h.author ?? "").split(":")[0]] ?? h.author ?? "—"}</td>
                          <td style={{ fontSize: 11 }}>{h.created_at}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </details>
            </>
          )}
        </div>
      )}

      {/* ============ ② 平台分项计划 ============ */}
      {tab === "platforms" && (
        <div className="panel">
          <div className="panel-head">
            <div>
              <div className="panel-title"><span className="icon">⌗</span>平台分项计划</div>
              <div className="panel-sub">
                {currentRoundId
                  ? `第 ${roundDetail?.round_number ?? currentRoundId} 轮的逐平台变体（每选题 × 每平台一篇）`
                  : "选择一轮后展示逐平台变体"}
              </div>
            </div>
            <RoundPicker rounds={rounds} value={currentRoundId} onChange={setCurrentRoundId} />
          </div>

          {!currentRoundId && (
            <EmptyHint next="点右上角「＋ 创建新一轮」，创建后回到本页选择轮次">暂无轮次</EmptyHint>
          )}

          {currentRoundId && (
            <div className="grid two">
              {ALL_PLATFORMS.map((p) => {
                const items = contents.filter((c) => c.platform === p);
                const cfgEnabled = plan?.per_platform?.[p]?.enabled ?? selPlatforms.includes(p);
                return (
                  <div key={p} style={{ border: "1px solid var(--line)", borderRadius: 10, padding: 12 }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                      <div style={{ fontWeight: 700, fontSize: 13 }}>{PLATFORM_NAME[p]}</div>
                      <span className={`tag ${cfgEnabled ? "green" : "gray"}`}>
                        {cfgEnabled ? "计划内启用" : "未启用"}
                      </span>
                    </div>
                    <div className="note" style={{ marginBottom: 8 }}>
                      目标 {plan?.per_platform?.[p]?.topic_count ?? planTopicCount} 个选题 × 1 篇；本平台已产出 {items.length} 篇
                    </div>
                    {items.length === 0 && (
                      <EmptyHint next="到「③ 生产线」选中本轮并点「▶ 运行本轮」">本轮暂无该平台内容</EmptyHint>
                    )}
                    <div className="list">
                      {items.map((c) => (
                        <div key={c.id} className="task-row" style={{ cursor: "pointer" }}
                             onClick={() => { setSelectedContentId(c.id); setTab("review"); }}>
                          <i className={`task-prio ${GUARD_CLS[c.guard_outcome ?? ""] ?? "gray"}`} />
                          <div className="task-main">
                            <div className="task-title">{c.title}</div>
                            <div className="task-desc">
                              {c.topic_id ?? "—"} · {c.word_count ?? "—"} 字 · 卡片 {c.image_count} 张
                              {c.draft_status && <> · 草稿 <span className={`tag ${STATUS_CLS[c.draft_status] ?? "gray"}`}>{c.draft_status}</span></>}
                            </div>
                          </div>
                          <span className={`tag ${GUARD_CLS[c.guard_outcome ?? ""] ?? "gray"}`}>{c.guard_outcome ?? "—"}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* ============ ③ 生产线 ============ */}
      {tab === "pipeline" && (
        <div className="grid two">
          <div className="panel" style={{ marginBottom: 0 }}>
            <div className="panel-head">
              <div>
                <div className="panel-title"><span className="icon">▶</span>运行本轮</div>
                <div className="panel-sub">选题 → 文案生成 → 终极目标检查 → 图片卡片 → 保存草稿</div>
              </div>
              <RoundPicker rounds={rounds} value={currentRoundId} onChange={setCurrentRoundId} />
            </div>

            {!roundDetail && (
              <EmptyHint next="在右上角轮次下拉框选择一轮；没有轮次就先点页面右上角「＋ 创建新一轮」">尚未选择轮次</EmptyHint>
            )}

            {roundDetail && (
              <>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
                  <div style={{ fontWeight: 700 }}>第 {roundDetail.round_number} 轮</div>
                  <span className={`tag ${STATUS_CLS[roundDetail.status]}`}>{ROUND_STATUS_LABEL[roundDetail.status] ?? roundDetail.status}</span>
                </div>

                {noLogin.length > 0 && (
                  <div style={{ marginBottom: 8 }}>
                    <DemoBadge
                      level="blocked"
                      text={`${noLogin.map((p) => PLATFORM_NAME[p]).join("、")} 未连接真实账号，存草稿会失败（勾选存草稿前请先登录）`}
                    />
                  </div>
                )}

                <label className="flex" style={{ gap: 6, alignItems: "center", fontSize: 12, marginBottom: 10 }}>
                  <input type="checkbox" checked={usePlan} onChange={(e) => setUsePlan(e.target.checked)} disabled={running} />
                  <b>按母计划 + Agent 编排执行</b>
                  <span style={{ color: "var(--muted)" }}>
                    {plan ? `（每平台 ${plan.plan.topic_count_per_platform} 篇 · ${(plan.plan.enabled_platforms ?? []).map((x) => PLATFORM_NAME[x as Platform]).join("、")}${Object.keys(plan.plan.dimension_allocation ?? {}).length ? " · 含维度配额" : ""}）` : ""}
                  </span>
                </label>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "center", opacity: usePlan ? 0.5 : 1 }}
                  aria-disabled={usePlan}>
                  <label style={{ fontSize: 12, color: "var(--muted)" }}>
                    选题数
                    <input
                      type="number" min={1} max={20} value={topicCount}
                      onChange={(e) => setTopicCount(Math.max(1, Math.min(20, Number(e.target.value) || 1)))}
                      style={{ width: 64, marginLeft: 6 }}
                      disabled={running || usePlan}
                    />
                  </label>
                  <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                    {ALL_PLATFORMS.map((p) => (
                      <button
                        key={p}
                        className={`btn sm ${selPlatforms.includes(p) ? "primary" : ""}`}
                        onClick={() => togglePlatform(p)}
                        disabled={running || usePlan}
                      >
                        {PLATFORM_NAME[p]}
                      </button>
                    ))}
                  </div>
                  <label style={{ fontSize: 12, color: "var(--muted)", display: "flex", alignItems: "center", gap: 4 }}>
                    <input type="checkbox" checked={saveDrafts} onChange={(e) => setSaveDrafts(e.target.checked)} disabled={running || usePlan} />
                    执行存草稿
                  </label>
                </div>
                <div style={{ marginTop: 10 }}>
                  <button className="btn primary" onClick={handleRun} disabled={!canRun}>
                    {running ? "◷ 运行中…" : "▶ 运行本轮"}
                  </button>
                </div>

                {roundDetail.status !== "created" && (
                  <div className="note" style={{ marginTop: 8 }}>
                    只有 created 状态的轮次可运行（当前 {roundDetail.status}）。如需重跑，请新建一轮。
                  </div>
                )}
                {!usePlan && selPlatforms.length === 0 && <div className="note" style={{ marginTop: 8, color: "var(--red)" }}>至少选择一个平台</div>}
                {runError && <div className="note" style={{ marginTop: 8, color: "var(--red)" }}>! {runError}</div>}

                {report && (
                  <div style={{ marginTop: 16 }}>
                    <div className="panel-title" style={{ fontSize: 13, marginBottom: 10 }}>
                      执行报告 · 状态 {report.final_status ?? "—"}
                    </div>
                    <div className="grid four" style={{ marginBottom: 10 }}>
                      <MiniStat label="内容产出" value={report.contents_created} />
                      <MiniStat label="草稿写入" value={report.drafts_created} />
                      <MiniStat label="存草稿成功" value={report.drafts_saved} tone="green" />
                      <MiniStat label="目标检查拦截" value={report.guard_rejected} tone={report.guard_rejected > 0 ? "orange" : undefined} />
                    </div>
                    {report.retry_counts && Object.keys(report.retry_counts).length > 0 && (
                      <div className="note" style={{ marginBottom: 8 }}>
                        重试次数：
                        {Object.entries(report.retry_counts).map(([k, v]) => `${k} × ${v}`).join(" · ")}
                      </div>
                    )}
                    <div className="table-wrap">
                      <table>
                        <thead><tr><th>阶段</th><th>结果</th></tr></thead>
                        <tbody>
                          {report.steps.map((s, i) => (
                            <tr key={i}>
                              <td style={{ fontFamily: "monospace", fontSize: 11 }}>{s.step}</td>
                              <td style={{ fontSize: 11 }}>
                                {s.step === "score" && `${(s.selected as unknown[])?.length ?? 0} 条选题入选`}
                                {s.step === "kb_read" && `${s.count} 条选题库记录`}
                                {s.step === "produce" && `产出 ${s.contents} 篇内容 · 目标检查拦截 ${s.guard_rejected} 次`}
                                {s.step === "dry_run" && `${s.note}`}
                                {s.step === "save_drafts" && `成功 ${s.saved} · 失败 ${s.failed}`}
                                {s.step === "guard_reject" && `${s.platform} / ${s.topic_id} · ${s.result} · ${s.reason}`}
                                {s.step === "plan_signature" && `签名 ${s.signature}`}
                                {s.step === "draft_save" && `${s.platform} · ${s.status ?? ""} ${s.error ?? ""}`}
                                {s.step === "apply_feedback" && `吃进复盘结论：${Object.keys((s.dimension_bias as object) ?? {}).length} 个维度调整 · ${s.copy_hint_count} 条文案要点`}
                                {s.step === "dimension_quota" && `维度配额 ${Object.entries((s.allocation as Record<string, number>) ?? {}).map(([d, n]) => `${d}×${n}`).join("、")}`}
                                {s.step === "auto_schedule" && `自动排期 ${s.scheduled} 篇`}
                                {!["score", "kb_read", "produce", "dry_run", "save_drafts", "apply_feedback", "dimension_quota",
                                   "auto_schedule", "guard_reject", "plan_signature", "draft_save"].includes(s.step) && "—"}
                              </td>
                            </tr>
                          ))}
                          {report.steps.length === 0 && (
                            <tr><td colSpan={2}>
                              <EmptyHint className="" next="重新点「▶ 运行本轮」查看执行报告；若仍为空，请检查后端日志">无阶段记录</EmptyHint>
                            </td></tr>
                          )}
                        </tbody>
                      </table>
                    </div>
                    {report.errors.length > 0 && (
                      <div className="note" style={{ marginTop: 8, color: "var(--red)" }}>
                        ! {report.errors.join("；")}
                      </div>
                    )}
                  </div>
                )}
              </>
            )}
          </div>

          {/* 阶段计数 */}
          <div className="panel" style={{ marginBottom: 0 }}>
            <div className="panel-head">
              <div>
                <div className="panel-title"><span className="icon">⎇</span>阶段计数</div>
                <div className="panel-sub">从 DB 实际落库数据回读（不是前端估算）</div>
              </div>
            </div>

            {!pipeline && (
              <EmptyHint next="在右上角轮次下拉框选择一轮，运行后会从 DB 回读各阶段计数">尚未选择轮次</EmptyHint>
            )}

            {pipeline && (
              <>
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 12 }}>
                  {pipeline.stages.map((s) => (
                    <div key={s.key} className="kpi-card" style={{ flex: "1 1 100px", padding: 10 }}>
                      <div style={{ fontSize: 11, color: "var(--muted)" }}>{s.label}</div>
                      <div style={{ fontSize: 22, fontWeight: 800 }}>{s.count}</div>
                    </div>
                  ))}
                </div>
                <div className="note" style={{ marginBottom: 10 }}>
                  终极目标检查累计拦截 {pipeline.guard_rejections ?? "—"} 次（含重生成前）；
                  草稿状态：
                  {Object.keys(pipeline.drafts_by_status).length === 0
                    ? " 无"
                    : ` ${Object.entries(pipeline.drafts_by_status).map(([k, v]) => `${k}=${v}`).join(" · ")}`}
                </div>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr><th>平台</th><th>文案</th><th>目标检查通过</th><th>卡片</th><th>草稿已存</th><th>待存</th><th>失败</th></tr>
                    </thead>
                    <tbody>
                      {pipeline.per_platform.length === 0 && (
                        <tr><td colSpan={7}>
                          <EmptyHint className="" next="点左侧「▶ 运行本轮」，内容落库后这里会显示各阶段计数">本轮还没有内容落库</EmptyHint>
                        </td></tr>
                      )}
                      {pipeline.per_platform.map((r) => (
                        <tr key={r.platform}>
                          <td>{PLATFORM_NAME[r.platform] ?? r.platform}</td>
                          <td>{r.contents}</td>
                          <td>{r.guard_pass}</td>
                          <td>{r.cards}</td>
                          <td style={{ color: "var(--green)" }}>{r.draft_saved}</td>
                          <td>{r.draft_pending}</td>
                          <td style={{ color: r.draft_failed > 0 ? "var(--red)" : undefined }}>{r.draft_failed}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="note" style={{ marginTop: 8 }}>
                  说明：文案数 = 通过终极目标检查后落库的数量；被拦截并丢弃的文案不会出现在这里，只计入「检查累计拦截」。
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {/* ============ ④ 内容编辑与审核 ============ */}
      {tab === "review" && (
        <div className="review-layout">
          {/* 左：本轮内容列表 */}
          <div className="panel">
            <div className="panel-head">
              <div>
                <div className="panel-title"><span className="icon">✎</span>本轮内容（{filteredReviewContents.length}/{contents.length}）</div>
                <div className="panel-sub">按关键词、平台和状态快速定位；点击内容后在右侧编辑</div>
              </div>
            </div>
            <RoundPicker rounds={rounds} value={currentRoundId} onChange={setCurrentRoundId} />
            <div className="review-filters">
              <input aria-label="搜索本轮内容" value={reviewSearch} onChange={(event) => setReviewSearch(event.target.value)} placeholder="搜索标题、正文、标签…" />
              <select aria-label="按平台筛选" value={reviewPlatform} onChange={(event) => setReviewPlatform(event.target.value as "all" | Platform)}>
                <option value="all">全部平台</option><option value="xhs">小红书</option><option value="douyin">抖音</option>
                <option value="kuaishou">快手</option><option value="wechat_channels">视频号</option>
              </select>
              <select aria-label="按状态筛选" value={reviewState} onChange={(event) => setReviewState(event.target.value)}>
                <option value="all">全部状态</option><option value="needs_review">需要复核</option>
                <option value="not_saved">待存草稿</option><option value="saved">已存草稿</option><option value="rejected">已驳回</option>
              </select>
            </div>
            <div className="list review-content-list">
              {contents.length === 0 && (
                <EmptyHint next="到「③ 生产线」选中本轮并点「▶ 运行本轮」">本轮暂无内容</EmptyHint>
              )}
              {contents.length > 0 && filteredReviewContents.length === 0 && (
                <div className="empty-state">没有符合筛选条件的内容。清空关键词或改用其他筛选条件。</div>
              )}
              {filteredReviewContents.map((c) => (
                <div
                  key={c.id}
                  className="task-row"
                  style={{
                    cursor: "pointer",
                    border: selectedContentId === c.id ? "2px solid var(--blue)" : "2px solid transparent",
                  }}
                  onClick={() => setSelectedContentId(c.id)}
                >
                  <i className={`task-prio ${GUARD_CLS[c.guard_outcome ?? ""] ?? "gray"}`} />
                  <div className="task-main">
                    <div className="task-title">{c.title}</div>
                    <div className="task-desc">
                      {PLATFORM_NAME[c.platform]} · 检查 {c.guard_outcome ?? "—"} · 卡片 {c.image_count} 张
                      {c.draft_status && <> · <span className={`tag ${STATUS_CLS[c.draft_status] ?? "gray"}`}>{c.draft_status}</span></>}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* 中：文案编辑 */}
          <div className="panel">
            <div className="panel-head">
              <div>
                <div className="panel-title"><span className="icon">⌕</span>编辑与重检</div>
                <div className="panel-sub">修改文案后必须重新过目标检查，检查器异常不会默认通过</div>
              </div>
            </div>

            {!selectedContent && (
              <EmptyHint next="在左侧「本轮内容」列表点击一篇文案">尚未选择内容</EmptyHint>
            )}

            {selectedContent && (
              <>
                <div className="note" style={{ marginBottom: 8 }}>
                  {PLATFORM_NAME[selectedContent.platform]} · {selectedContent.topic_id ?? "—"} · 卡片 {selectedContent.image_count} 张
                  {selectedContent.draft_error && (
                    <span style={{ color: "var(--orange)" }}> · {selectedContent.draft_error}</span>
                  )}
                </div>

                <label style={{ fontSize: 12, color: "var(--muted)" }}>标题</label>
                <input value={editTitle} onChange={(e) => setEditTitle(e.target.value)} style={{ width: "100%", marginBottom: 8 }} />

                <label style={{ fontSize: 12, color: "var(--muted)" }}>正文</label>
                <textarea
                  value={editBody}
                  onChange={(e) => setEditBody(e.target.value)}
                  rows={12}
                  style={{ width: "100%", marginBottom: 8, fontFamily: "inherit", resize: "vertical" }}
                />

                <label style={{ fontSize: 12, color: "var(--muted)" }}>标签（空格或逗号分隔）</label>
                <input value={editTags} onChange={(e) => setEditTags(e.target.value)} style={{ width: "100%", marginBottom: 10 }} />

                <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 10 }}>
                  <button className="btn primary" onClick={handleSaveContent} disabled={isPending("save-content")}>
                    {isPending("save-content") ? "提交中…" : "✓ 保存修改"}
                  </button>
                  <button className="btn" onClick={handleRecheck} disabled={isPending("recheck-content")}>
                    {isPending("recheck-content") ? "重检中…" : "⌕ 重新过目标检查"}
                  </button>
                  <button className="btn" onClick={() => {
                    const url = new URL(window.location.href);
                    url.searchParams.set("page", "assets");
                    url.searchParams.set("tab", "media");
                    url.searchParams.set("focusContent", selectedContent.id);
                    window.history.pushState({}, "", url.toString());
                    window.dispatchEvent(new PopStateEvent("popstate"));
                  }}>▧ 在素材库为此内容配图</button>
                  <button className="btn" onClick={handleCopy}>☷ 复制文案</button>
                  <button className="btn danger" style={{ marginLeft: "auto" }} onClick={handleDeleteContent}
                    disabled={isPending("delete-content") || ["user_published", "data_fetching", "published"].includes(selectedContent.draft_status ?? "")}
                    title="已发布（有数据）的内容不能删除">
                    {isPending("delete-content") ? "删除中…" : "删除这篇"}
                  </button>
                </div>

                {(selectedContent.platform === "xhs" || selectedContent.platform === "douyin") && (
                  <div className="note" style={{ marginBottom: 10, fontSize: 12 }}>
                    ● {selectedContent.platform === "xhs" ? "小红书「暂存离开」" : "抖音「暂存离开」"}草稿存于发布浏览器本地（localStorage `publish_cache`），浏览器关闭即消失（G-36）。
                    用「复制文案」+「下载卡片」在自己的 App 里手动发布即可。
                  </div>
                )}

                {recheck && (
                  <div
                    className="note"
                    style={{
                      marginBottom: 10,
                      borderLeft: `4px solid ${recheck.passes ? "var(--green)" : "var(--red)"}`,
                      paddingLeft: 8,
                    }}
                  >
                    重检结果：<b>{recheck.result}</b> · {recheck.passes ? "通过" : "未通过"}
                    {recheck.reason && <> · 原因：{recheck.reason}</>}
                    {recheck.redline_hits.length > 0 && <> · 红线命中：{recheck.redline_hits.join("、")}</>}
                  </div>
                )}

                <div style={{ borderTop: "1px dashed var(--line)", paddingTop: 10 }}>
                  <div className="panel-title" style={{ fontSize: 12, marginBottom: 6 }}>驳回（记录理由，不自动改草稿状态）</div>
                  <div style={{ display: "flex", gap: 8 }}>
                    <input
                      value={rejectReason} placeholder="例如：观点与账号定位不符"
                      onChange={(e) => setRejectReason(e.target.value)}
                      style={{ flex: 1 }}
                    />
                    <button className="btn" onClick={handleReject} disabled={isPending("reject-content")}>
                      {isPending("reject-content") ? "提交中…" : "✕ 驳回"}
                    </button>
                  </div>
                </div>

                <div className="note" style={{ marginTop: 12 }}>
                  当前记录：终极目标检查 = <b>{selectedContent.guard_outcome ?? "—"}</b>
                  {selectedContent.guard_reason && <>（{selectedContent.guard_reason}）</>}
                  {" · "}正文 {selectedContent.word_count ?? "—"} 字 · 草稿 {selectedContent.draft_status ?? "—"}
                </div>
              </>
            )}
          </div>

          {/* 右：卡片图预览 */}
          <div className="panel review-cardcol">
            <div className="panel-head">
              <div>
                <div className="panel-title">
                  <span className="icon">▤</span>卡片预览（{selectedContent?.image_count ?? 0}）
                </div>
                <div className="panel-sub">封面 + 正文分页，点图看原图</div>
              </div>
              {selectedContent && (
                <button className="btn sm" onClick={handleRegenerateCards} disabled={isPending("regen-cards")}>
                  {isPending("regen-cards") ? "生成中…" : "↻ 重新生成"}
                </button>
              )}
            </div>

            {!selectedContent && (
              <EmptyHint next="在左侧「本轮内容」列表点击一篇文案">尚未选择内容</EmptyHint>
            )}

            {selectedContent && selectedContent.image_count === 0 && (
              <EmptyHint next="点右上角「↻ 重新生成」，按当前标题/正文出图">本篇还没有卡片图</EmptyHint>
            )}

            {selectedContent && selectedContent.image_count > 0 && (
              <>
                <div className="card-grid">
                  {Array.from({ length: selectedContent.image_count }, (_, i) => {
                    const url = api.contentImageUrl(selectedContent.id, i);
                    return (
                      <div className="card-item" key={i}>
                        {brokenCards[i] ? (
                          <div className="card-thumb is-broken">
                            图片文件已丢失<br />点右上角重新生成
                          </div>
                        ) : (
                          <button
                            type="button"
                            className="card-thumb"
                            onClick={() => setPreviewCard(i)}
                            title={`预览第 ${i + 1} 张`}
                          >
                            <img
                              src={url}
                              alt={`卡片 ${i + 1}`}
                              onError={() => setBrokenCards((prev) => ({ ...prev, [i]: true }))}
                            />
                          </button>
                        )}
                        <div className="card-meta">
                          <span>第 {i + 1} 张</span>
                          <a
                            className="btn sm"
                            href={api.contentImageUrl(selectedContent.id, i, true)}
                            download
                            style={{ textDecoration: "none" }}
                          >⬇ 下载</a>
                        </div>
                      </div>
                    );
                  })}
                </div>
                <div className="note" style={{ marginTop: 10, fontSize: 12 }}>
                  点缩略图可看大图预览；卡片图按当前标题 / 正文生成，改完文案后点「↻ 重新生成」，发布时按顺序上传。
                </div>
              </>
            )}
          </div>

          {/* 卡片大图预览层（点击缩略图打开，Esc / 点遮罩关闭） */}
          {selectedContent && previewCard !== null && (
            <div className="card-lightbox" onClick={() => setPreviewCard(null)} role="dialog" aria-modal="true">
              <div className="card-lightbox-inner" onClick={(e) => e.stopPropagation()}>
                <div className="card-lightbox-head">
                  <span>
                    {PLATFORM_NAME[selectedContent.platform]} · 第 {previewCard + 1} / {selectedContent.image_count} 张
                  </span>
                  <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                    {selectedContent.image_count > 1 && (
                      <>
                        <button
                          className="btn sm"
                          onClick={() => setPreviewCard((previewCard - 1 + selectedContent.image_count) % selectedContent.image_count)}
                        >‹ 上一张</button>
                        <button
                          className="btn sm"
                          onClick={() => setPreviewCard((previewCard + 1) % selectedContent.image_count)}
                        >下一张 ›</button>
                      </>
                    )}
                    <a
                      className="btn sm"
                      href={api.contentImageUrl(selectedContent.id, previewCard, true)}
                      download
                      style={{ textDecoration: "none" }}
                    >⬇ 下载这张</a>
                    <button className="btn sm" onClick={() => setPreviewCard(null)}>✕ 关闭</button>
                  </div>
                </div>
                <img
                  src={api.contentImageUrl(selectedContent.id, previewCard)}
                  alt={`卡片 ${previewCard + 1}`}
                />
              </div>
            </div>
          )}
        </div>
      )}

      {/* ============ ⑤ 轮次状态 ============ */}
      {tab === "rounds" && (
        <div className="grid two">
          <div className="panel" style={{ marginBottom: 0 }}>
            <div className="panel-head">
              <div>
                <div className="panel-title"><span className="icon">≡</span>轮次列表</div>
                <div className="panel-sub">Round 1 · Round 2 · ...</div>
              </div>
            </div>
            <div className="list">
              {rounds.length === 0 && (
                <EmptyHint next="点右上角「＋ 创建新一轮」，创建后即可运行主流程">暂无轮次</EmptyHint>
              )}
              {rounds.map((r) => (
                <div
                  key={r.id}
                  className="task-row"
                  style={{ cursor: "pointer", border: currentRoundId === r.id ? "2px solid var(--blue)" : "2px solid transparent" }}
                  onClick={() => { setCurrentRoundId(r.id); setReport(null); }}
                >
                  <i className={`task-prio ${STATUS_CLS[r.status]}`} />
                  <div className="task-main">
                    <div className="task-title">第 {r.round_number} 轮</div>
                    <div className="task-desc">创建于 {new Date(r.created_at).toLocaleString("zh-CN")}</div>
                  </div>
                  <span className={`tag ${STATUS_CLS[r.status]}`}>{ROUND_STATUS_LABEL[r.status] ?? r.status}</span>
                </div>
              ))}
            </div>
          </div>

          <div className="panel" style={{ marginBottom: 0 }}>
            <div className="panel-head">
              <div>
                <div className="panel-title">
                  <span className="icon">⎇</span>当前轮次
                  {roundDetail && <span className={`tag ${STATUS_CLS[roundDetail.status]}`} style={{ marginLeft: 10 }}>{roundDetail.status}</span>}
                </div>
                <div className="panel-sub">合法迁移按钮由后端状态机校验生成</div>
              </div>
            </div>

            {!roundDetail && (
              <EmptyHint next="在左侧「轮次列表」点击一轮；列表为空就先点右上角「＋ 创建新一轮」">尚未选择轮次</EmptyHint>
            )}

            {roundDetail && (
              <>
                <div className="round-track">
                  {ROUND_STATUSES.map((s, i) => {
                    const isCurrent = s === roundDetail.status;
                    const currentIdx = ROUND_STATUSES.indexOf(roundDetail.status);
                    const isPast = i < currentIdx;
                    return (
                      <Fragment key={s}>
                        {i > 0 && <div className={`track-connector ${isPast ? "completed" : ""}`} />}
                        <div className={`track-node ${isCurrent ? "active" : ""} ${isPast ? "completed" : ""}`}>
                          <div className="dot" />
                          <div className="label">{s}</div>
                        </div>
                      </Fragment>
                    );
                  })}
                </div>

                <div className="note" style={{ marginTop: 12 }}>
                  计划版本：{roundDetail.plan_signature
                    ? <span style={{ fontFamily: "monospace" }}>{roundDetail.plan_signature.slice(0, 16)}…</span>
                    : "—"}
                  {(roundDetail as any).guard_rejections != null && <> · 目标检查拦截 {(roundDetail as any).guard_rejections} 次</>}
                </div>

                <div style={{ marginTop: 14, display: "flex", flexWrap: "wrap", gap: 8 }}>
                  <span style={{ fontSize: 11, color: "var(--muted)", marginRight: 4 }}>可迁移到：</span>
                  {(ROUND_TARGETABLE[roundDetail.status] || []).map((t) => (
                    <button key={t} className="btn sm" onClick={() => handleTransition(t)}>→ {t}</button>
                  ))}
                  {(ROUND_TARGETABLE[roundDetail.status] || []).length === 0 && (
                    <span style={{ fontSize: 12, color: "var(--subtle)" }}>当前状态无合法迁移（终态）</span>
                  )}
                  <button className="btn sm danger" style={{ marginLeft: "auto" }} onClick={handleDeleteRound}
                    disabled={isPending("delete-round") || ["producing", "collecting_metrics", "analyzing"].includes(roundDetail.status)}
                    title="进行中的轮次不能删除；有已发布内容的轮次后端会拒绝">
                    {isPending("delete-round") ? "删除中…" : "删除本轮"}
                  </button>
                </div>

                <div style={{ marginTop: 22 }}>
                  <div className="panel-title" style={{ fontSize: 13, marginBottom: 10 }}>✎ 单篇草稿（{roundDetail.drafts?.length || 0}）</div>
                  <div className="table-wrap">
                    <table>
                      <thead><tr><th>草稿 ID</th><th>平台</th><th>状态</th><th>操作</th></tr></thead>
                      <tbody>
                        {roundDetail.drafts?.length === 0 && (
                          <tr><td colSpan={4}>
                            <EmptyHint className="" next="到「③ 生产线」点「▶ 运行本轮」生成内容并存草稿">该轮次暂无草稿</EmptyHint>
                          </td></tr>
                        )}
                        {roundDetail.drafts?.map((d) => (
                          <tr key={d.id}>
                            <td style={{ fontFamily: "monospace", fontSize: 11 }}>{d.id.slice(0, 16)}...</td>
                            <td>{PLATFORM_NAME[d.platform] ?? d.platform}</td>
                            <td><span className={`tag ${STATUS_CLS[d.status] || "gray"}`}>{d.status}</span></td>
                            <td>
                              {d.status === "draft_saved" && (
                                <button className="btn sm primary" onClick={() => confirmPublish(d.id)}>✓ 我已发布</button>
                              )}
                              {(d.status === "draft_saved" || d.status === "draft_pending") && (
                                <button className="btn sm ghost" style={{ marginLeft: 6 }} onClick={() => cancelDraft(d.id)}>✕ 不发了</button>
                              )}
                              {d.status !== "draft_saved" && d.status !== "draft_pending" && <span style={{ color: "var(--subtle)" }}>—</span>}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </>
  );
}

function RoundPicker({ rounds, value, onChange }: {
  rounds: SpiralRound[];
  value: number | null;
  onChange: (id: number) => void;
}) {
  if (rounds.length === 0) return null;
  return (
    <select
      value={value ?? ""}
      onChange={(e) => onChange(Number(e.target.value))}
      style={{ fontSize: 12 }}
    >
      {rounds.map((r) => (
        <option key={r.id} value={r.id}>第 {r.round_number} 轮 · {ROUND_STATUS_LABEL[r.status] ?? r.status}</option>
      ))}
    </select>
  );
}

function MiniStat({ label, value, tone }: { label: string; value: number; tone?: string }) {
  const color = tone === "green" ? "var(--green)" : tone === "orange" ? "var(--orange)" : "var(--text)";
  return (
    <div className="kpi-card" style={{ padding: 10 }}>
      <div style={{ fontSize: 11, color: "var(--muted)" }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 800, color }}>{value}</div>
    </div>
  );
}
