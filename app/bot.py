"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { ChevronLeft } from "lucide-react";
import { Field } from "@/components/sections/auth-card";
import { createClient } from "@/lib/supabase/client";
import { cn } from "@/lib/utils";

type Tone = { formality: string; emoji: string; language: string; length: string };
type PersonaForm = {
  persona_name: string;
  greeting: string;
  role_bio: string;
  topics: string;
  limitations: string;
  custom_instructions: string;
  store_info: string;
  disclose_ai: boolean;
  voice_enabled: boolean;
  cmd_takeover_stop: string;
  cmd_takeover_start: string;
  cmd_global_stop: string;
  cmd_global_start: string;
  tone_config: Tone;
  hours_enabled: boolean;
  hours_start: number;
  hours_end: number;
  tz_offset: number;
  away_message: string;
};

const DEFAULTS: PersonaForm = {
  persona_name: "Assistant",
  greeting: "",
  role_bio: "the store assistant",
  topics: "",
  limitations: "",
  custom_instructions: "",
  store_info: "",
  disclose_ai: true,
  voice_enabled: true,
  cmd_takeover_stop: "//stop",
  cmd_takeover_start: "//start",
  cmd_global_stop: "//stopall",
  cmd_global_start: "//startall",
  tone_config: { formality: "balanced", emoji: "light", language: "auto", length: "medium" },
  hours_enabled: false,
  hours_start: 9,
  hours_end: 22,
  tz_offset: 6,
  away_message: "",
};

export default function SettingsPage() {
  const [form, setForm] = useState<PersonaForm>(DEFAULTS);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [note, setNote] = useState<{ ok: boolean; msg: string } | null>(null);

  useEffect(() => {
    (async () => {
      const supabase = createClient();
      const { data: { user } } = await supabase.auth.getUser();
      if (!user) return;
      const { data } = await supabase.from("personas").select("*").eq("user_id", user.id).maybeSingle();
      if (data) {
        setForm({
          ...DEFAULTS,
          ...data,
          tone_config: { ...DEFAULTS.tone_config, ...(data.tone_config ?? {}) },
        });
      }
      setLoading(false);
    })();
  }, []);

  function set<K extends keyof PersonaForm>(key: K, value: PersonaForm[K]) {
    setForm((f) => ({ ...f, [key]: value }));
  }
  function setTone<K extends keyof Tone>(key: K, value: string) {
    setForm((f) => ({ ...f, tone_config: { ...f.tone_config, [key]: value } }));
  }

  async function save() {
    setNote(null);
    const cmds = [form.cmd_takeover_stop, form.cmd_takeover_start, form.cmd_global_stop, form.cmd_global_start].map((c) => c.trim());
    if (cmds.some((c) => !c)) return setNote({ ok: false, msg: "Commands can't be empty." });
    if (new Set(cmds).size !== 4) return setNote({ ok: false, msg: "All four commands must be different." });

    setSaving(true);
    const supabase = createClient();
    const { data: { user } } = await supabase.auth.getUser();
    if (!user) { setSaving(false); return setNote({ ok: false, msg: "Not signed in." }); }

    const { error } = await supabase.from("personas").upsert({ user_id: user.id, ...form, updated_at: new Date().toISOString() });
    setSaving(false);
    setNote(error ? { ok: false, msg: error.message } : { ok: true, msg: "Saved." });
  }

  if (loading) return <section className="dash"><p className="muted">Loading…</p></section>;

  return (
    <section className="dash">
      <Link href="/dashboard" className="back-link"><ChevronLeft size={16} /> Back to dashboard</Link>
      <h1 className="dash-title">Persona & tone</h1>
      <p className="muted" style={{ marginTop: 8 }}>How your assistant talks and what it&apos;s allowed to do.</p>

      <div style={{ marginTop: 32 }}>
        <div className="panel glass">
          <p className="panel-title">Identity</p>
          <p className="panel-desc">The name and role it uses with customers.</p>
          <div className="form-grid">
            <Field label="Assistant name" value={form.persona_name} onChange={(e) => set("persona_name", e.target.value)} />
            <Field label="Role" value={form.role_bio} onChange={(e) => set("role_bio", e.target.value)} />
          </div>
          <div style={{ marginTop: 16 }}>
            <label style={{ display: "block" }}>
              <span className="field-label">Greeting (optional)</span>
              <input className="field-input" value={form.greeting} onChange={(e) => set("greeting", e.target.value)} placeholder="Hey! How can I help today?" />
            </label>
          </div>
        </div>

        <div className="panel glass">
          <p className="panel-title">Tone</p>
          <p className="panel-desc">Match how you actually talk to customers.</p>
          <div className="form-grid">
            <Select label="Formality" value={form.tone_config.formality} onChange={(v) => setTone("formality", v)} options={[["casual", "Casual"], ["balanced", "Balanced"], ["formal", "Formal"]]} />
            <Select label="Emoji" value={form.tone_config.emoji} onChange={(v) => setTone("emoji", v)} options={[["none", "None"], ["light", "Light"], ["heavy", "Lots"]]} />
            <Select label="Language" value={form.tone_config.language} onChange={(v) => setTone("language", v)} options={[["auto", "Match the customer"], ["english", "English"], ["bangla", "Bangla"], ["banglish", "Banglish"]]} />
            <Select label="Reply length" value={form.tone_config.length} onChange={(v) => setTone("length", v)} options={[["short", "Short"], ["medium", "Medium"], ["detailed", "Detailed"]]} />
          </div>
        </div>

        <div className="panel glass">
          <p className="panel-title">Knowledge & limits</p>
          <p className="panel-desc">What it should talk about, and what it must never do.</p>
          <Textarea label="Topics it handles" value={form.topics} onChange={(v) => set("topics", v)} placeholder="Products, pricing, payment, delivery, refunds…" />
          <Textarea label="Store info" value={form.store_info} onChange={(v) => set("store_info", v)} placeholder="Payment methods, delivery time, refund policy…" />
          <Textarea label="Limitations (never do)" value={form.limitations} onChange={(v) => set("limitations", v)} placeholder="Never invent prices. Never ask for card numbers or OTP. Escalate refunds to a human." />
          <Textarea label="Custom instructions" value={form.custom_instructions} onChange={(v) => set("custom_instructions", v)} placeholder="Anything else about how it should behave." />
        </div>

        <div className="panel glass">
          <p className="panel-title">Take-over commands</p>
          <p className="panel-desc">Type these in a chat to control the assistant. All four must be different.</p>
          <div className="form-grid">
            <Field label="Pause for this customer" value={form.cmd_takeover_stop} onChange={(e) => set("cmd_takeover_stop", e.target.value)} />
            <Field label="Resume this customer" value={form.cmd_takeover_start} onChange={(e) => set("cmd_takeover_start", e.target.value)} />
            <Field label="Pause everything" value={form.cmd_global_stop} onChange={(e) => set("cmd_global_stop", e.target.value)} />
            <Field label="Resume everything" value={form.cmd_global_start} onChange={(e) => set("cmd_global_start", e.target.value)} />
          </div>
        </div>

        <div className="panel glass">
          <p className="panel-title">Business hours</p>
          <p className="panel-desc">Optional. Reply only during set hours; outside them, send an away message.</p>
          <Toggle label="Enable business hours" desc="When off, the bot replies 24/7." on={form.hours_enabled} onToggle={() => set("hours_enabled", !form.hours_enabled)} />
          {form.hours_enabled && (
            <>
              <div className="form-grid" style={{ marginTop: 12 }}>
                <label style={{ display: "block" }}><span className="field-label">Active from (hour 0–23)</span><input className="field-input" type="number" min={0} max={23} value={form.hours_start} onChange={(e) => set("hours_start", Number(e.target.value))} /></label>
                <label style={{ display: "block" }}><span className="field-label">Active until (hour 0–23)</span><input className="field-input" type="number" min={0} max={23} value={form.hours_end} onChange={(e) => set("hours_end", Number(e.target.value))} /></label>
              </div>
              <label style={{ display: "block", marginTop: 16 }}><span className="field-label">Timezone offset from UTC (Bangladesh = 6)</span><input className="field-input" type="number" value={form.tz_offset} onChange={(e) => set("tz_offset", Number(e.target.value))} /></label>
              <Textarea label="Away message (outside hours)" value={form.away_message} onChange={(v) => set("away_message", v)} placeholder="Thanks! We're offline right now — we'll reply during business hours 🙂" />
            </>
          )}
        </div>

        <div className="panel glass">
          <p className="panel-title">Behavior</p>
          <Toggle label="Be honest it's an assistant" desc="Recommended — safer and keeps trust." on={form.disclose_ai} onToggle={() => set("disclose_ai", !form.disclose_ai)} />
          <Toggle label="Allow voice replies" desc="Only sends voice when a customer asks for it." on={form.voice_enabled} onToggle={() => set("voice_enabled", !form.voice_enabled)} />
        </div>

        <div className="save-bar">
          <button className="btn btn-primary" onClick={save} disabled={saving}>{saving ? "Saving…" : "Save changes"}</button>
          {note && <span className={cn("save-note", note.ok ? "ok" : "err")}>{note.msg}</span>}
        </div>
      </div>
    </section>
  );
}

function Select({ label, value, onChange, options }: { label: string; value: string; onChange: (v: string) => void; options: [string, string][] }) {
  return (
    <label style={{ display: "block" }}>
      <span className="field-label">{label}</span>
      <select className="field-select" value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
      </select>
    </label>
  );
}

function Textarea({ label, value, onChange, placeholder }: { label: string; value: string; onChange: (v: string) => void; placeholder?: string }) {
  return (
    <label style={{ display: "block", marginTop: 16 }}>
      <span className="field-label">{label}</span>
      <textarea className="field-textarea" value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} />
    </label>
  );
}

function Toggle({ label, desc, on, onToggle }: { label: string; desc: string; on: boolean; onToggle: () => void }) {
  return (
    <div className="switch-row">
      <div>
        <div>{label}</div>
        <div className="muted" style={{ fontSize: 13, marginTop: 2 }}>{desc}</div>
      </div>
      <button type="button" className={cn("switch", on && "on")} onClick={onToggle} aria-pressed={on}>
        <span className="switch-knob" />
      </button>
    </div>
  );
}
