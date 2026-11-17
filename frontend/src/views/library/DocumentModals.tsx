import { useEffect, useState } from "react";
import { FileText, Globe, LoaderCircle, Plus } from "lucide-react";
import { Modal } from "../../components/Modal";
import { addTextDocument, addUrlDocument, toUserMessage } from "../../lib/api";
import { useApp } from "../../state/AppContext";
import type { DocumentItem } from "../../types";

interface DocumentModalsProps {
  noteOpen: boolean;
  urlOpen: boolean;
  onCloseNote: () => void;
  onCloseUrl: () => void;
  onSaved: (document: DocumentItem) => void;
}

function parseTags(raw: string): string[] {
  return raw
    .split(/[,，、\s]+/)
    .map((tag) => tag.trim())
    .filter(Boolean)
    .slice(0, 8);
}

export function AddNoteDialog({
  open,
  onClose,
  onSaved,
}: {
  open: boolean;
  onClose: () => void;
  onSaved: (document: DocumentItem) => void;
}) {
  const { toast } = useApp();
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [tags, setTags] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (open) {
      setTitle("");
      setContent("");
      setTags("");
      setError(null);
      setSaving(false);
    }
  }, [open]);

  const submit = async (): Promise<void> => {
    if (saving) return;
    const trimmedTitle = title.trim();
    const trimmedContent = content.trim();
    if (!trimmedTitle) {
      setError("请填写笔记标题。");
      return;
    }
    if (trimmedContent.length < 10) {
      setError("笔记内容至少需要 10 个字。");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const result = await addTextDocument({
        title: trimmedTitle,
        content: trimmedContent,
        tags: parseTags(tags),
        source_type: "note",
      });
      toast("success", "笔记已添加", `《${trimmedTitle}》已进入知识库并建立知识连接。`);
      onSaved(result.document);
      onClose();
    } catch (err) {
      setError(toUserMessage(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="添加笔记"
      description="直接在知脉中记录想法，稍后就能聊天引用它。"
      icon={<FileText size={17} />}
      width={580}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={saving}>
            取消
          </button>
          <button type="submit" form="add-note-form" className="btn btn-primary" disabled={saving}>
            {saving ? <LoaderCircle size={14} className="spin" /> : <Plus size={14} />}
            {saving ? "保存中…" : "保存笔记"}
          </button>
        </>
      }
    >
      <form
        id="add-note-form"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
        style={{ display: "flex", flexDirection: "column", gap: 16 }}
      >
        <label className="field">
          <span className="field-label">标题</span>
          <input
            className="input"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="例如：周末阅读方法总结"
            maxLength={120}
            autoFocus
          />
        </label>
        <label className="field">
          <span className="field-label">
            <span>内容</span>
            <span className="field-hint">{content.length} 字</span>
          </span>
          <textarea
            className="textarea"
            value={content}
            onChange={(event) => setContent(event.target.value)}
            placeholder="写下你的笔记、想法或摘录…"
            rows={9}
          />
        </label>
        <label className="field">
          <span className="field-label">
            <span>标签</span>
            <span className="field-hint">用逗号分隔，可留空</span>
          </span>
          <input
            className="input"
            value={tags}
            onChange={(event) => setTags(event.target.value)}
            placeholder="例如：学习方法, 阅读"
          />
        </label>
        {error ? <div className="field-error">{error}</div> : null}
      </form>
    </Modal>
  );
}

export function AddUrlDialog({
  open,
  onClose,
  onSaved,
}: {
  open: boolean;
  onClose: () => void;
  onSaved: (document: DocumentItem) => void;
}) {
  const { toast } = useApp();
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [tags, setTags] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (open) {
      setUrl("");
      setTitle("");
      setTags("");
      setError(null);
      setLoading(false);
    }
  }, [open]);

  const submit = async (): Promise<void> => {
    if (loading) return;
    const trimmedUrl = url.trim();
    if (!/^https?:\/\/.+/i.test(trimmedUrl)) {
      setError("请输入合法的 http/https 网页地址。");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const result = await addUrlDocument({
        url: trimmedUrl,
        title: title.trim() || null,
        tags: parseTags(tags),
      });
      toast("success", "网页已导入", `《${result.document.title}》已抓取正文并建立索引。`);
      onSaved(result.document);
      onClose();
    } catch (err) {
      setError(toUserMessage(err));
    } finally {
      setLoading(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="添加网页"
      description="输入一个网页地址，知脉会抓取正文并加入知识库。"
      icon={<Globe size={17} />}
      width={580}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={loading}>
            取消
          </button>
          <button type="submit" form="add-url-form" className="btn btn-primary" disabled={loading}>
            {loading ? <LoaderCircle size={14} className="spin" /> : <Globe size={14} />}
            {loading ? "抓取中…" : "抓取并保存"}
          </button>
        </>
      }
    >
      <form
        id="add-url-form"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
        style={{ display: "flex", flexDirection: "column", gap: 16 }}
      >
        <label className="field">
          <span className="field-label">网页地址</span>
          <input
            className="input"
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            placeholder="https://example.com/article"
            autoFocus
            spellCheck={false}
          />
        </label>
        <label className="field">
          <span className="field-label">
            <span>标题</span>
            <span className="field-hint">留空则自动读取网页标题</span>
          </span>
          <input
            className="input"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="可选"
            maxLength={120}
          />
        </label>
        <label className="field">
          <span className="field-label">
            <span>标签</span>
            <span className="field-hint">用逗号分隔，可留空</span>
          </span>
          <input
            className="input"
            value={tags}
            onChange={(event) => setTags(event.target.value)}
            placeholder="例如：技术写作, 参考"
          />
        </label>
        {error ? <div className="field-error">{error}</div> : null}
        <div className="field-hint">
          仅支持 http/https 地址，页面正文不超过 5 MB；抓取失败时会返回具体原因。
        </div>
      </form>
    </Modal>
  );
}

export function DocumentModals({ noteOpen, urlOpen, onCloseNote, onCloseUrl, onSaved }: DocumentModalsProps) {
  return (
    <>
      <AddNoteDialog open={noteOpen} onClose={onCloseNote} onSaved={onSaved} />
      <AddUrlDialog open={urlOpen} onClose={onCloseUrl} onSaved={onSaved} />
    </>
  );
}
