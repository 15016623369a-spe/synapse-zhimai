import {
  ArrowRight,
  BookOpen,
  GitBranch,
  Compass,
  Layers,
  RefreshCw,
  Sparkles,
  Tag,
  WifiOff,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { NeuralOrb } from "../../components/Logo";
import { Skeleton } from "../../components/Skeleton";
import { navigate } from "../../lib/router";
import { formatNumber } from "../../lib/format";
import { useApp } from "../../state/AppContext";

const QUICK_PROMPTS: { icon: LucideIcon; text: string }[] = [
  { icon: Sparkles, text: "我的知识库里都有哪些主题？" },
  { icon: GitBranch, text: "RAG 的检索流程是怎样的？" },
  { icon: BookOpen, text: "如何用卡片盒笔记法整理知识？" },
  { icon: Compass, text: "怎么安排一次舒服的京都旅行？" },
];

interface WelcomePanelProps {
  onAsk: (question: string) => void;
  disabled?: boolean;
}

export function WelcomePanel({ onAsk, disabled }: WelcomePanelProps) {
  const { stats, statsLoading, statsError, refreshStats, health, healthError, refreshHealth } =
    useApp();

  const llmConfigured = Boolean(health?.llm.configured);
  const connectionDown = Boolean(healthError) || Boolean(statsError && !stats);

  return (
    <div className="welcome">
      <div className="welcome-orb-wrap">
        <NeuralOrb size={124} />
      </div>

      <div className="welcome-eyebrow">Synapse · 你的第二大脑</div>

      <h1 className="welcome-title">
        你好，我是<span className="gradient-text">知脉</span>。
      </h1>

      <div className="welcome-desc">
        {statsLoading ? (
          <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
            正在连接你的知识库
            <Skeleton width={42} height={13} radius={5} />
            <Skeleton width={42} height={13} radius={5} />
          </span>
        ) : stats ? (
          <>
            已连接你的 <span className="accent">{formatNumber(stats.documents)}</span> 份文档、
            <span className="accent">{formatNumber(stats.concepts)}</span> 个知识节点。
            问我任何关于你知识库的问题。
          </>
        ) : (
          <>已准备好倾听你的知识库。问我任何问题，我会尽力从你的文档中找到答案。</>
        )}
      </div>

      {stats ? (
        <div className="welcome-kpis">
          <div className="welcome-kpi violet">
            <Layers size={13} />
            <b className="mono">{formatNumber(stats.chunks)}</b>
            <span>文本分块</span>
          </div>
          <div className="welcome-kpi amber">
            <Tag size={13} />
            <b className="mono">{formatNumber(stats.tags.length)}</b>
            <span>主题标签</span>
          </div>
          <div className="welcome-kpi mint">
            <GitBranch size={13} />
            <b className="mono">{formatNumber(stats.links)}</b>
            <span>知识连接</span>
          </div>
          {statsError ? (
            <button type="button" className="welcome-stat-retry" onClick={() => void refreshStats()}>
              <RefreshCw size={11} /> 刷新
            </button>
          ) : null}
        </div>
      ) : null}

      {connectionDown ? (
        <div className="welcome-connect-error">
          <WifiOff size={14} />
          <span>无法连接知脉服务，请确认后端已启动。</span>
          <button
            type="button"
            onClick={() => {
              void refreshHealth();
              void refreshStats();
            }}
          >
            重试
          </button>
        </div>
      ) : null}

      <div className="welcome-chips">
        {QUICK_PROMPTS.map((prompt) => {
          const Icon = prompt.icon;
          return (
            <button
              key={prompt.text}
              type="button"
              className="quick-chip"
              onClick={() => onAsk(prompt.text)}
              disabled={disabled}
            >
              <span className="quick-chip-icon">
                <Icon size={15} />
              </span>
              <span>{prompt.text}</span>
              <ArrowRight size={14} className="quick-chip-arrow" />
            </button>
          );
        })}
      </div>

      {!llmConfigured ? (
        <div className="welcome-hint">
          <Sparkles size={13} />
          <span>
            当前为本地检索模式 —— 配置大模型后可获得更自然的回答。
            <button type="button" onClick={() => navigate("/settings")}>
              去配置
            </button>
          </span>
        </div>
      ) : (
        <div className="welcome-hint welcome-hint-ai">
          <Sparkles size={13} />
          <span>
            已接入模型 <b className="mono">{health?.llm.model || "自定义模型"}</b>
            ，回答由 AI 生成并标注来源。
          </span>
        </div>
      )}
    </div>
  );
}
