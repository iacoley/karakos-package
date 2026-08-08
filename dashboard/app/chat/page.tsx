"use client";

import { useState, useEffect, useRef, FormEvent, KeyboardEvent } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { usePoll } from "@/lib/hooks";

interface AgentList {
  // /api/agents returns { agents: [...] }, an array of { name, state, ... }
  // objects -- not a dict keyed by name. This was previously typed as
  // Record<string, {state}> and read via Object.keys(agentData.agents),
  // which on an array yields numeric indices ("0", "1", ...) instead of
  // agent names. The dropdown looked populated but was silently wrong.
  agents: { name: string; state: string }[];
}

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  ts: string;
}

const MAX_TEXTAREA_ROWS = 8;

export default function ChatPage() {
  const { data: agentData } = usePoll<AgentList>("/api/agents", 30000);
  const [agent, setAgent] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [reloading, setReloading] = useState(false);
  const [reloadMsg, setReloadMsg] = useState<string | null>(null);
  const [openingTerminal, setOpeningTerminal] = useState(false);
  const messagesEnd = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const agents = agentData?.agents ? agentData.agents.map((a) => a.name) : [];

  // Set default agent
  useEffect(() => {
    if (agents.length > 0 && !agent) {
      setAgent(agents[0]);
    }
  }, [agents, agent]);

  useEffect(() => {
    messagesEnd.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function handleOpenTerminal() {
    if (!agent || openingTerminal) return;
    setOpeningTerminal(true);
    setReloadMsg(`Opening ${agent} in Terminal…`);
    try {
      const res = await fetch(`/api/agents/${encodeURIComponent(agent)}/open-terminal`, {
        method: "POST",
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: "Request failed" }));
        setReloadMsg(`Terminal failed: ${err.error || res.statusText}`);
      } else {
        setReloadMsg(`${agent} opened in Terminal.`);
        setTimeout(() => setReloadMsg(null), 4000);
      }
    } catch (err) {
      setReloadMsg(`Terminal error: ${err instanceof Error ? err.message : "unknown"}`);
    } finally {
      setOpeningTerminal(false);
    }
  }

  async function handleReload() {
    if (!agent || reloading || streaming) return;
    setReloading(true);
    setReloadMsg(`Reloading ${agent}…`);
    try {
      const res = await fetch(`/api/agents/${encodeURIComponent(agent)}/reload`, {
        method: "POST",
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: "Request failed" }));
        setReloadMsg(`Reload failed: ${err.error || res.statusText}`);
      } else {
        setReloadMsg(`${agent} reloaded — session preserved.`);
        setTimeout(() => setReloadMsg(null), 4000);
      }
    } catch (err) {
      setReloadMsg(`Reload error: ${err instanceof Error ? err.message : "unknown"}`);
    } finally {
      setReloading(false);
    }
  }

  // Seed messages from server-side history when an agent is selected. The
  // chat page kept no record across reloads even though message_queue stored
  // every turn — pulling the last N entries restores continuity.
  useEffect(() => {
    if (!agent) return;
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(
          `/api/chat/history?agent=${encodeURIComponent(agent)}&limit=50`
        );
        if (!res.ok) return;
        const data = await res.json();
        if (cancelled) return;
        const seeded: ChatMessage[] = (data.messages || []).map(
          (m: { role: "user" | "assistant"; content: string; ts: string }) => ({
            role: m.role,
            content: m.content,
            ts: m.ts,
          })
        );
        setMessages(seeded);
      } catch {
        // ignore — empty chat is acceptable failure mode
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [agent]);

  // Auto-resize textarea up to MAX_TEXTAREA_ROWS lines.
  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    const lineHeight = parseFloat(getComputedStyle(ta).lineHeight) || 24;
    const padding =
      parseFloat(getComputedStyle(ta).paddingTop) +
      parseFloat(getComputedStyle(ta).paddingBottom);
    const max = lineHeight * MAX_TEXTAREA_ROWS + padding;
    ta.style.height = Math.min(ta.scrollHeight, max) + "px";
  }, [input]);

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends, Shift-Enter inserts newline. Respect IME composition so
    // multi-keystroke input methods (Japanese, Chinese, Korean) don't fire
    // a send while a candidate is being chosen.
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      handleSend(e as unknown as FormEvent);
    }
  }

  async function handleSend(e: FormEvent) {
    e.preventDefault();
    if (!input.trim() || !agent || streaming) return;

    const userMsg: ChatMessage = { role: "user", content: input, ts: new Date().toISOString() };
    setMessages((prev) => [...prev, userMsg]);
    const userInput = input;
    setInput("");
    setStreaming(true);

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent, content: userInput }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: "Request failed" }));
        setMessages((prev) => [...prev, { role: "assistant", content: `Error: ${err.error}`, ts: new Date().toISOString() }]);
        setStreaming(false);
        return;
      }

      const data = await res.json();
      const messageId = data.message_id;

      // Add placeholder for streaming response
      setMessages((prev) => [...prev, { role: "assistant", content: "", ts: new Date().toISOString() }]);

      // Subscribe to SSE stream
      const eventSource = new EventSource(`/api/chat/stream?message_id=${messageId}`);

      eventSource.onmessage = (event) => {
        const payload = JSON.parse(event.data);
        if (payload.done) {
          eventSource.close();
          setStreaming(false);
        } else if (payload.chunk) {
          setMessages((prev) =>
            prev.map((m, i) =>
              i === prev.length - 1 && m.role === "assistant"
                ? { ...m, content: m.content + payload.chunk }
                : m
            )
          );
        }
      };

      eventSource.onerror = () => {
        eventSource.close();
        setStreaming(false);
      };
    } catch (err) {
      setMessages((prev) => [...prev, {
        role: "assistant",
        content: `Connection error: ${err instanceof Error ? err.message : "unknown"}`,
        ts: new Date().toISOString(),
      }]);
      setStreaming(false);
    }
  }

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-4 mb-4 flex-shrink-0">
        <h1 className="text-2xl font-semibold">Chat</h1>
        <select
          value={agent}
          onChange={(e) => { setAgent(e.target.value); setMessages([]); }}
          className="px-3 py-2 bg-gray-900 border border-gray-800 rounded text-gray-100 focus:outline-none focus:border-gray-600"
        >
          {agents.map((a) => (
            <option key={a} value={a}>{a}</option>
          ))}
        </select>
        <button
          type="button"
          onClick={handleReload}
          disabled={reloading || streaming || !agent}
          title="Bounce subprocess, preserve session — picks up SYSTEM_PROMPT / persona / MCP changes"
          className="px-3 py-2 bg-gray-900 hover:bg-gray-800 disabled:opacity-50 border border-gray-800 rounded text-gray-300 text-sm transition-colors"
        >
          {reloading ? "Reloading…" : "↻ Reload"}
        </button>
        <button
          type="button"
          onClick={handleOpenTerminal}
          disabled={openingTerminal || !agent}
          title="Open this agent in a Terminal window (macOS only) — REPL with slash commands, mirrors into this chat log"
          className="px-3 py-2 bg-gray-900 hover:bg-gray-800 disabled:opacity-50 border border-gray-800 rounded text-gray-300 text-sm transition-colors"
        >
          {openingTerminal ? "Opening…" : "⌘ Terminal"}
        </button>
        {reloadMsg && (
          <span className="text-xs text-gray-400">{reloadMsg}</span>
        )}
      </div>

      <div className="flex-1 overflow-auto py-2 mb-4 min-h-0">
        {messages.map((msg, i) => {
          const isLastStreaming =
            streaming && i === messages.length - 1 && msg.role === "assistant";
          return (
            <div
              key={i}
              className={`p-3 mb-2 rounded-lg ${
                msg.role === "assistant"
                  ? "bg-gray-900 border border-gray-800"
                  : "bg-gray-950"
              }`}
            >
              <strong
                className={`text-xs block mb-1 ${
                  msg.role === "user" ? "text-gray-100" : "text-blue-400"
                }`}
              >
                {msg.role === "user" ? "You" : agent}
              </strong>
              {msg.role === "user" ? (
                <p className="text-sm whitespace-pre-wrap text-gray-300">
                  {msg.content}
                </p>
              ) : (
                <div className="text-sm text-gray-300 chat-markdown">
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm]}
                    components={{
                      a: (props) => (
                        <a
                          {...props}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-blue-400 underline hover:text-blue-300"
                        />
                      ),
                      code: ({ className, children, ...props }) => {
                        const isBlock = /\n/.test(String(children ?? ""));
                        if (isBlock) {
                          return (
                            <code
                              className={`block bg-gray-950 border border-gray-800 rounded px-3 py-2 my-2 overflow-x-auto font-mono text-xs ${className ?? ""}`}
                              {...props}
                            >
                              {children}
                            </code>
                          );
                        }
                        return (
                          <code
                            className="bg-gray-950 border border-gray-800 rounded px-1 py-0.5 font-mono text-xs"
                            {...props}
                          >
                            {children}
                          </code>
                        );
                      },
                      pre: ({ children }) => <>{children}</>,
                      ul: (props) => (
                        <ul className="list-disc pl-5 my-3 space-y-1" {...props} />
                      ),
                      ol: (props) => (
                        <ol className="list-decimal pl-5 my-3 space-y-1" {...props} />
                      ),
                      h1: (props) => (
                        <h1 className="text-lg font-semibold mt-4 mb-2" {...props} />
                      ),
                      h2: (props) => (
                        <h2 className="text-base font-semibold mt-4 mb-2" {...props} />
                      ),
                      h3: (props) => (
                        <h3 className="text-sm font-semibold mt-3 mb-2" {...props} />
                      ),
                      p: (props) => (
                        <p className="my-3 leading-relaxed" {...props} />
                      ),
                      blockquote: (props) => (
                        <blockquote
                          className="border-l-2 border-gray-700 pl-3 my-2 text-gray-400"
                          {...props}
                        />
                      ),
                      table: (props) => (
                        <table
                          className="my-2 border-collapse border border-gray-800 text-xs"
                          {...props}
                        />
                      ),
                      th: (props) => (
                        <th
                          className="border border-gray-800 px-2 py-1 bg-gray-950 font-semibold text-left"
                          {...props}
                        />
                      ),
                      td: (props) => (
                        <td className="border border-gray-800 px-2 py-1" {...props} />
                      ),
                      hr: (props) => (
                        <hr className="border-gray-800 my-3" {...props} />
                      ),
                    }}
                  >
                    {msg.content}
                  </ReactMarkdown>
                  {isLastStreaming && (
                    <span className="opacity-50 inline-block">▌</span>
                  )}
                </div>
              )}
            </div>
          );
        })}
        <div ref={messagesEnd} />
      </div>

      <form
        onSubmit={handleSend}
        className="flex gap-2 pt-2 border-t border-gray-800 items-end flex-shrink-0"
      >
        <textarea
          ref={textareaRef}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={`Message ${agent || "agent"}...  (Shift-Enter for newline)`}
          disabled={streaming}
          rows={1}
          className="flex-1 px-3 py-2 bg-gray-900 border border-gray-800 rounded text-gray-100 text-sm leading-6 resize-none focus:outline-none focus:border-gray-600 disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={streaming || !input.trim()}
          className="px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:bg-gray-700 text-white rounded transition-colors"
        >
          Send
        </button>
      </form>
    </div>
  );
}
