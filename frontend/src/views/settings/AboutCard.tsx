import { AtSign, Github, Info, Mail } from "lucide-react";

/** 关于知脉（设置页底部信息卡）。 */
export function AboutCard() {
  return (
    <section className="card settings-card">
      <div className="settings-card-head">
        <div className="settings-card-icon amber">
          <Info size={18} />
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h2 className="settings-card-title">关于知脉</h2>
          <p className="settings-card-desc">让知识，像神经元一样连接。</p>
        </div>
        <span className="badge">v2.4.0</span>
      </div>
      <div className="settings-body">
        <div className="about-stack">
          <span className="about-label">技术栈</span>
          <div className="about-chips">
            {["React 19", "TypeScript", "Vite 6", "d3-force", "FastAPI", "SQLite"].map((item) => (
              <span key={item} className="tag-chip">
                {item}
              </span>
            ))}
          </div>
        </div>
        <div className="about-stack">
          <span className="about-label">快捷键</span>
          <div className="shortcut-list">
            <div className="shortcut-row">
              <span>命令面板</span>
              <span>
                <span className="kbd">⌘</span> <span className="kbd">K</span>
              </span>
            </div>
            <div className="shortcut-row">
              <span>发送消息</span>
              <span className="kbd">Enter</span>
            </div>
            <div className="shortcut-row">
              <span>换行</span>
              <span>
                <span className="kbd">Shift</span> + <span className="kbd">Enter</span>
              </span>
            </div>
            <div className="shortcut-row">
              <span>关闭浮层</span>
              <span className="kbd">Esc</span>
            </div>
            <div className="shortcut-row">
              <span>图谱缩放 / 平移</span>
              <span className="shortcut-dim">滚轮 / 拖拽</span>
            </div>
            <div className="shortcut-row">
              <span>释放固定节点</span>
              <span className="shortcut-dim">双击节点</span>
            </div>
          </div>
        </div>
        <div className="about-stack">
          <span className="about-label">联系作者</span>
          <div className="about-links">
            <a
              className="about-link"
              href="https://x.com/JaceHuang123"
              target="_blank"
              rel="noreferrer"
            >
              <AtSign size={13} /> @JaceHuang123
            </a>
            <a className="about-link" href="mailto:15016623369a@gmail.com">
              <Mail size={13} /> 15016623369a@gmail.com
            </a>
            <a
              className="about-link"
              href="https://github.com/15016623369a-spe/synapse-zhimai"
              target="_blank"
              rel="noreferrer"
            >
              <Github size={13} /> synapse-zhimai
            </a>
          </div>
        </div>
        <div className="about-footer">
          <span>Synapse · 知脉</span>
          <span className="mono">build 2026.11</span>
        </div>
      </div>
    </section>
  );
}
