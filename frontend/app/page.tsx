"use client";

import { useState, useEffect, useId } from "react";
import {
  ArrowRight,
  ArrowUpRight,
  ShieldCheck,
  Cpu,
  BrainCircuit,
  Trophy,
  Fingerprint,
  Zap,
  Code2,
  Lock,
  Activity,
  ChevronRight,
  Globe,
  Database,
  Menu,
  X,
  Check,
  Sparkles,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";



/* =====================================
   SCROLL REVEAL ANIMATION
===================================== */

function Reveal({
  children,
  className = "",
  delay = 0,
}: {
  children: React.ReactNode;
  className?: string;
  delay?: number;
}) {
  const [visible, setVisible] = useState(false);

  const id = useId();

  useEffect(() => {
    const element = document.querySelector(
      `[data-reveal-id="${id}"]`
    );

    if (!element) return;

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setVisible(true);
          observer.unobserve(entry.target);
        }
      },
      { threshold: 0.12 }
    );

    observer.observe(element);

    return () => observer.disconnect();
  }, [id]);

  return (
    <div
      data-reveal-id={id}
      className={`reveal-element ${visible ? "reveal-visible" : ""} ${className}`}
      style={{ transitionDelay: `${delay}ms` }}
    >
      {children}
    </div>
  );
}



const features = [
  {
    id: "01",
    icon: BrainCircuit,
    title: "AI & Peer Review Engine",
    description:
      "Submit real projects. Get AI-powered analysis and verified feedback from elite developers.",
    color: "#00F0FF",
    tags: ["AST ANALYSIS", "PEER VALIDATION"],
  },
  {
    id: "02",
    icon: Zap,
    title: "Dynamic Reputation Scoring",
    description:
      "Earn XP, unlock ranks and build an undeniable developer reputation.",
    color: "#B026FF",
    tags: ["LIVE XP", "RANK SYSTEM"],
  },
  {
    id: "03",
    icon: Fingerprint,
    title: "The Living Skill Passport",
    description:
      "Turn your achievements into a cryptographically verifiable identity.",
    color: "#00FF66",
    tags: ["VERIFIED ID", "CRYPTO READY"],
  },
  {
    id: "04",
    icon: Trophy,
    title: "Global Hacker Leaderboard",
    description:
      "Compete against the best developers and climb the global rankings.",
    color: "#FFB547",
    tags: ["GLOBAL RANK", "LIVE BATTLES"],
  },
];

const users = [
  { rank: "01", name: "Neo_Cipher", xp: "24,890", score: 98, color: "#00F0FF" },
  { rank: "02", name: "Alex_Dev", xp: "18,420", score: 82, color: "#B026FF" },
  { rank: "03", name: "BytePhantom", xp: "15,210", score: 68, color: "#00FF66" },
];

function Label({ children }: { children: React.ReactNode }) {
  return (
    <div className="mb-6 flex items-center gap-3 font-mono text-[10px] tracking-[.25em] text-cyan-400">
      <span className="h-px w-8 bg-cyan-400" />
      {children}
      <span className="h-px flex-1 bg-cyan-400/20" />
    </div>
  );
}

function TerminalWidget({
  title,
  value,
  subtitle,
  color,
  icon: Icon,
}: {
  title: string;
  value: string;
  subtitle: string;
  color: string;
  icon: LucideIcon;
}) {
  return (
    <div className="glass-card float-card rounded-xl p-6">
      <div className="flex items-center justify-between">
        <span
          className="flex items-center gap-2 font-mono text-xs"
          style={{ color }}
        >
          <Icon size={16} />
          {title}
        </span>
        <span className="h-2 w-2 animate-pulse rounded-full bg-green-400" />
      </div>

      <div className="mt-6 text-4xl font-black text-white">{value}</div>

      <p className="mt-3 font-mono text-[10px] text-zinc-500">
        {subtitle}
      </p>

      <div className="mt-5 h-1 rounded-full bg-white/10">
        <div
          className="h-full w-[85%] rounded-full"
          style={{ background: color }}
        />
      </div>
    </div>
  );
}

function AccessForm() {
  const [email, setEmail] = useState("");
  const [consentAccepted, setConsentAccepted] = useState(false);
  const [submissionState, setSubmissionState] = useState<
    "idle" | "submitting" | "accepted"
  >("idle");
  const [error, setError] = useState("");
  const [serverMessage, setServerMessage] = useState("");

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setError("");

    const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/+$/, "");
    const consentVersion = process.env.NEXT_PUBLIC_CONSENT_VERSION;

    if (!consentAccepted) {
      setError("CONSENT_REQUIRED: Please accept the waitlist consent notice.");
      return;
    }

    if (!apiBase || !consentVersion) {
      setError("CONFIG_ERROR: Registration service configuration is missing.");
      return;
    }

    setSubmissionState("submitting");

    try {
      const response = await fetch(`${apiBase}/api/v1/waitlist/register`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          email: email.trim(),
          consent_accepted: true,
          consent_version: consentVersion,
        }),
      });

      const data = (await response.json().catch(() => null)) as {
        message?: unknown;
      } | null;

      if (response.status === 202) {
        setServerMessage(
          typeof data?.message === "string"
            ? data.message
            : "Your waitlist request has been received."
        );
        setSubmissionState("accepted");
        return;
      }

      switch (response.status) {
        case 409:
          setError(
            "CONSENT_UPDATED: The consent notice has changed. Reload the page and review it before submitting again."
          );
          break;

        case 422:
          setError("VALIDATION_ERROR: Please enter a valid email address.");
          break;

        case 429:
          setError("RATE_LIMITED: Too many attempts. Please try again later.");
          break;

        case 503:
          setError(
            "SERVICE_UNAVAILABLE: Registration is temporarily unavailable. Please try again shortly."
          );
          break;

        default:
          setError(
            `REQUEST_FAILED: Registration could not be completed (HTTP ${response.status}).`
          );
      }
    } catch {
      setError(
        "CONNECTION_ERROR: Could not reach SkillBridge. Check that the backend server is running."
      );
    } finally {
      setSubmissionState((current) =>
        current === "submitting" ? "idle" : current
      );
    }
  }

  const isSubmitting = submissionState === "submitting";
  const isAccepted = submissionState === "accepted";

  return (
    <div className="w-full max-w-xl">
      <form
        onSubmit={submit}
        className="access-box flex flex-col gap-4 rounded-xl p-3 sm:p-4"
      >
        <div className="flex flex-col gap-2 sm:flex-row">
          <div className="flex min-w-0 flex-1 items-center gap-3 px-3">
            <span className="font-mono text-cyan-400">&gt;_</span>

            <input
              type="email"
              name="email"
              required
              maxLength={320}
              autoComplete="email"
              disabled={isSubmitting || isAccepted}
              value={email}
              onChange={(e) => {
                setEmail(e.target.value);
                setError("");
              }}
              placeholder="enter_secure_email..."
              aria-label="Email address"
              className="w-full min-w-0 bg-transparent py-3 font-mono text-xs text-white outline-none placeholder:text-zinc-600 disabled:opacity-60"
            />
          </div>

          <button
            type="submit"
            disabled={isSubmitting || isAccepted}
            className="flex shrink-0 items-center justify-center gap-2 rounded-lg bg-cyan-400 px-5 py-4 font-mono text-xs font-bold text-black transition hover:bg-white disabled:cursor-not-allowed disabled:opacity-60"
          >
            {isSubmitting ? (
              "TRANSMITTING..."
            ) : isAccepted ? (
              <>
                REQUEST RECEIVED <Check size={14} />
              </>
            ) : (
              <>
                INITIALIZE ACCESS <ArrowRight size={14} />
              </>
            )}
          </button>
        </div>

        <label className="flex cursor-pointer items-start gap-3 px-3 text-xs leading-6 text-zinc-400">
          <input
            type="checkbox"
            name="consent_accepted"
            required
            disabled={isSubmitting || isAccepted}
            checked={consentAccepted}
            onChange={(e) => {
              setConsentAccepted(e.target.checked);
              setError("");
            }}
            className="mt-1 h-4 w-4 shrink-0 accent-cyan-400"
          />

          <span>
            I consent to SkillBridge using my email to process my waitlist
            registration and send verification-related emails.
            <span className="mt-1 block text-[10px] text-zinc-600">
              Required to submit your waitlist request.
            </span>
          </span>
        </label>

        {error && (
          <p role="alert" className="px-3 text-xs leading-6 text-red-400">
            {error}
          </p>
        )}
      </form>

      {isAccepted && (
        <div
          role="status"
          aria-live="polite"
          className="success-panel mt-4 rounded-xl border border-green-400/30 bg-green-900/20 p-5 backdrop-blur-xl"
        >
          <p className="font-mono text-xs text-green-400">
            <ShieldCheck className="mr-2 inline" size={16} />
            WAITLIST REQUEST RECEIVED
          </p>

          <p className="mt-4 break-words font-mono text-xs leading-6 text-green-300">
            {serverMessage}
          </p>

          {process.env.NEXT_PUBLIC_EMAIL_MODE === "console" && (
            <p className="mt-4 border-t border-green-400/20 pt-3 text-xs leading-6 text-amber-300">
              DEVELOPMENT MODE: Email delivery is not enabled yet. Your
              registration request is recorded, but no verification email
              will be delivered in this environment.
            </p>
          )}
        </div>
      )}

      <p className="mt-3 font-mono text-[9px] text-zinc-600">
        <Lock size={10} className="mr-1 inline" />
        SECURE CONNECTION // WAITLIST REGISTRATION
      </p>
    </div>
  );
}

function FeatureCard({ item }: { item: typeof features[number] }) {
  const Icon = item.icon;

  return (
    <div
      className="feature-card group rounded-2xl border border-white/10 bg-white/[0.02] p-7 transition-all duration-500 hover:-translate-y-2"
      style={{ "--accent": item.color } as React.CSSProperties}
    >
      <div className="flex justify-between font-mono text-xs text-zinc-600">
        NODE_{item.id}
        <ArrowUpRight size={18} />
      </div>

      <div
        className="my-8 flex h-14 w-14 items-center justify-center rounded-xl border border-white/10 bg-white/5"
        style={{ color: item.color }}
      >
        <Icon size={28} />
      </div>

      <h3 className="text-xl font-bold text-white">{item.title}</h3>

      <p className="mt-4 text-sm leading-7 text-zinc-500">
        {item.description}
      </p>

      <div className="mt-6 flex flex-wrap gap-2">
        {item.tags.map((tag) => (
          <span
            key={tag}
            className="rounded border border-white/10 px-2 py-1 font-mono text-[9px] text-zinc-400"
          >
            {tag}
          </span>
        ))}
      </div>

      <div
        className="mt-7 flex items-center gap-2 font-mono text-[10px]"
        style={{ color: item.color }}
      >
        EXPLORE NODE <ChevronRight size={14} />
      </div>
    </div>
  );
}

function Passport() {
  return (
    <div className="passport-card rounded-2xl border border-cyan-400/30 bg-[#07151a] p-6 sm:p-8">
      <div className="flex items-center justify-between">
        <span className="flex items-center gap-2 font-mono text-xs text-cyan-400">
          <Fingerprint size={18} />
          SKILL_PASSPORT.ID
        </span>

        <span className="rounded border border-green-400/30 px-2 py-1 font-mono text-[9px] text-green-400">
          VERIFIED
        </span>
      </div>

      <div className="mt-8 flex items-center gap-4">
        <div className="flex h-16 w-16 items-center justify-center rounded-xl border border-cyan-400/30 bg-cyan-400/10 text-3xl font-black text-cyan-300">
          A
        </div>

        <div>
          <h3 className="text-2xl font-black">Alex_Dev</h3>
          <p className="mt-1 font-mono text-[10px] text-zinc-500">
            ID: SB-8492-AX
          </p>
        </div>
      </div>

      <div className="my-8 grid grid-cols-2 gap-3">
        <div className="rounded-xl border border-white/10 bg-white/[0.03] p-4">
          <p className="font-mono text-[9px] text-zinc-500">
            SKILL SCORE
          </p>
          <div className="mt-2 text-4xl font-black text-cyan-300">
            98.4%
          </div>
        </div>

        <div className="rounded-xl border border-white/10 bg-white/[0.03] p-4">
          <p className="font-mono text-[9px] text-zinc-500">
            TOTAL XP
          </p>
          <div className="mt-2 text-4xl font-black text-purple-400">
            18.4K
          </div>
        </div>
      </div>

      <p className="mb-3 font-mono text-[10px] text-zinc-500">
        VERIFIED TECH STACK
      </p>

      <div className="flex flex-wrap gap-2">
        {["REACT", "NEXT.JS", "TYPESCRIPT", "PYTHON", "NODE.JS"].map((tag) => (
          <span
            key={tag}
            className="rounded border border-cyan-400/20 bg-cyan-400/5 px-3 py-2 font-mono text-[9px] text-cyan-300"
          >
            {tag}
          </span>
        ))}
      </div>

      <div className="mt-7 flex justify-between border-t border-white/10 pt-5 font-mono text-[9px]">
        <span className="text-zinc-600">CRYPTO SIGNATURE</span>
        <span className="text-green-400">0x8F2...A921</span>
      </div>
    </div>
  );
}

function Leaderboard() {
  return (
    <div className="rounded-2xl border border-purple-400/20 bg-[#100b19] p-6 sm:p-8">
      <div className="flex items-center justify-between">
        <span className="flex items-center gap-2 font-mono text-xs text-purple-400">
          <Trophy size={17} />
          GLOBAL_RANKING
        </span>

        <span className="animate-pulse text-[10px] text-green-400">
          ● LIVE
        </span>
      </div>

      <h3 className="mt-3 text-xl font-bold">Elite Developers</h3>

      <div className="mt-8 space-y-7">
        {users.map((user) => (
          <div key={user.rank}>
            <div className="grid grid-cols-[35px_1fr_auto] items-center gap-3">
              <span style={{ color: user.color }} className="font-mono">
                #{user.rank}
              </span>

              <span className="text-sm font-bold">{user.name}</span>

              <span className="font-mono text-xs">{user.xp} XP</span>
            </div>

            <div className="ml-12 mt-3 h-1 overflow-hidden rounded-full bg-white/5">
              <div
                className="xp-bar h-full rounded-full"
                style={{
                  width: `${user.score}%`,
                  background: user.color,
                }}
              />
            </div>
          </div>
        ))}
      </div>

      <button className="mt-9 flex w-full items-center justify-between rounded-lg border border-purple-400/20 bg-purple-400/5 p-4 font-mono text-[10px] text-purple-300">
        VIEW FULL LEADERBOARD
        <ArrowUpRight size={15} />
      </button>
    </div>
  );
}

export default function Home() {
  const [menu, setMenu] = useState(false);

  return (
    <main className="relative min-h-screen overflow-hidden bg-[#050505] text-white selection:bg-cyan-400 selection:text-black">

      {/* Background Effects */}
      <div className="grid-bg pointer-events-none fixed inset-0 opacity-30" />
      <div className="orb orb-blue" />
      <div className="orb orb-purple" />

      {/* NAVBAR */}
      <header className="sticky top-0 z-40 border-b border-white/10 bg-black/80 backdrop-blur-xl">
        <nav className="mx-auto flex max-w-7xl items-center justify-between px-5 py-4 lg:px-8">

          <a href="#" className="flex items-center gap-3 text-lg font-black">
            <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-cyan-400 text-black">
              <Code2 size={21} />
            </span>
            SKILL<span className="text-cyan-400">BRIDGE</span>
          </a>

          <div className="hidden gap-9 font-mono text-[10px] text-zinc-400 md:flex">
            <a href="#protocol" className="hover:text-cyan-400">PROTOCOL</a>
            <a href="#nodes" className="hover:text-cyan-400">NODES</a>
            <a href="#dashboard" className="hover:text-cyan-400">DASHBOARD</a>
          </div>

          <a
            href="#access"
            className="hidden rounded-md border border-cyan-400/40 px-4 py-3 font-mono text-[10px] text-cyan-300 md:block"
          >
            GET EARLY ACCESS ↗
          </a>

          <button
            aria-label="Toggle menu"
            onClick={() => setMenu(!menu)}
            className="text-cyan-400 md:hidden"
          >
            {menu ? <X /> : <Menu />}
          </button>
        </nav>

        {menu && (
          <div className="space-y-4 border-t border-white/10 bg-black p-6 font-mono text-xs md:hidden">
            <a onClick={() => setMenu(false)} href="#protocol" className="block">PROTOCOL</a>
            <a onClick={() => setMenu(false)} href="#nodes" className="block">NODES</a>
            <a onClick={() => setMenu(false)} href="#dashboard" className="block">DASHBOARD</a>
            <a onClick={() => setMenu(false)} href="#access" className="block text-cyan-400">GET ACCESS ↗</a>
          </div>
        )}
      </header>

      {/* HERO SECTION */}
      <section id="protocol" className="relative z-10 mx-auto max-w-7xl px-5 pb-24 pt-24 lg:px-8 lg:pt-32">

        <div className="mx-auto max-w-5xl text-center">

          <div className="inline-flex items-center gap-2 rounded-full border border-green-400/30 bg-green-400/5 px-4 py-2 font-mono text-[9px] tracking-widest text-green-400 sm:text-[10px]">
            <span className="h-2 w-2 animate-pulse rounded-full bg-green-400" />
            SECURE CONNECTION: PHASE 1 PRIVATE ALPHA
          </div>

          <h1 className="mt-10 text-5xl font-black leading-[1.08] tracking-tight sm:text-6xl md:text-7xl lg:text-[90px]">
            Prove Your Code.
            <br />
            <span className="gradient-text">Dominate the Ranks.</span>
          </h1>

          <p className="mx-auto mt-8 max-w-2xl text-sm leading-8 text-zinc-400 sm:text-base">
            The ultimate proving ground. Submit projects, get AI & peer reviews,
            forge your cryptographic Skill Passport, and climb the global leaderboard.
          </p>

          <div className="mt-7 flex flex-wrap justify-center gap-3">
            {["AI POWERED", "CRYPTOGRAPHIC ID", "GLOBAL XP SYSTEM"].map((x) => (
              <span
                key={x}
                className="rounded border border-white/10 bg-white/[0.03] px-3 py-2 font-mono text-[9px] text-zinc-400"
              >
                ✳ {x}
              </span>
            ))}
          </div>
        </div>

        {/* ACCESS + FLOATING WIDGETS */}
        <div
          id="access"
          className="mx-auto mt-20 grid max-w-6xl grid-cols-1 items-center gap-12 lg:grid-cols-[1.1fr_.9fr] lg:gap-16"
        >

          <div>
            <Label>INITIALIZE YOUR ACCESS</Label>

            <h2 className="mb-6 text-2xl font-bold sm:text-3xl">
              Your proof. Your power.{" "}
              <span className="text-cyan-400">Your future.</span>
            </h2>

            <AccessForm />
          </div>

          <div className="grid grid-cols-1 gap-5 sm:grid-cols-2 lg:grid-cols-1 xl:grid-cols-2">
            <TerminalWidget
              title="AST_AI_EVAL"
              value="98.4%"
              subtitle="> CODE_INTEGRITY: VERIFIED"
              color="#00F0FF"
              icon={Cpu}
            />

            <TerminalWidget
              title="XP_REPUTATION"
              value="12,840"
              subtitle="> RANK_TRAJECTORY: RISING"
              color="#B026FF"
              icon={Activity}
            />
          </div>
        </div>
      </section>

      {/* COMMAND CENTER */}
      <section
        id="nodes"
        className="relative z-10 border-y border-white/[0.06] bg-white/[0.012] py-24 sm:py-32"
      >
        <div className="mx-auto max-w-7xl px-5 lg:px-8">


<Reveal>
  <Label>PHASE_01 // COMMAND CENTER</Label>

  <h2 className="mb-5 text-4xl font-black sm:text-5xl">
    Four nodes.{" "}
    <span className="gradient-text">Infinite potential.</span>
  </h2>

  <p className="mb-12 max-w-xl text-sm leading-7 text-zinc-500">
    A new developer ecosystem engineered around actual ability,
    transparent validation and verifiable achievements.
  </p>
</Reveal>



<div className="grid grid-cols-1 gap-5 sm:grid-cols-2">
  {features.map((item, index) => (
    <Reveal key={item.id} delay={index * 120}>
      <FeatureCard item={item} />
    </Reveal>
  ))}
</div>

        </div>
      </section>

      {/* DASHBOARD */}
      <section id="dashboard" className="relative z-10 py-24 sm:py-32">
        <div className="mx-auto max-w-7xl px-5 lg:px-8">

          <Label>LIVE SIMULATION // MOCK_OS_001</Label>

          <div className="mb-12 flex flex-col justify-between gap-5 md:flex-row md:items-end">
            <div>

<Reveal>
  <Label>LIVE SIMULATION // MOCK_OS_001</Label>

  <div className="mb-12 flex flex-col justify-between gap-5 md:flex-row md:items-end">
    <div>
      <h2 className="text-4xl font-black sm:text-5xl">
        Inside the <span className="gradient-text">Matrix.</span>
      </h2>

      <p className="mt-4 max-w-lg text-sm leading-7 text-zinc-500">
        Your entire developer identity. Verified, ranked and ready to be discovered.
      </p>
    </div>

    <span className="w-fit rounded border border-green-400/30 bg-green-400/5 px-4 py-2 font-mono text-[10px] text-green-400">
      ● MOCK_ENVIRONMENT_ACTIVE
    </span>
  </div>
</Reveal>

            </div>

            <span className="w-fit rounded border border-green-400/30 bg-green-400/5 px-4 py-2 font-mono text-[10px] text-green-400">
              ● MOCK_ENVIRONMENT_ACTIVE
            </span>
          </div>

          <Reveal delay={150}>
          <div className="dashboard-window overflow-hidden rounded-2xl border border-white/10 bg-[#08090d]">

            {/* Window Header */}
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/10 bg-white/[0.025] px-5 py-4">

              <div className="flex items-center gap-2">
                <span className="h-2.5 w-2.5 rounded-full bg-red-500" />
                <span className="h-2.5 w-2.5 rounded-full bg-yellow-400" />
                <span className="h-2.5 w-2.5 rounded-full bg-green-400" />

                <span className="ml-3 font-mono text-[9px] tracking-widest text-zinc-500">
                  SKILLBRIDGE_OS / USER_DASHBOARD
                </span>
              </div>

              <span className="font-mono text-[9px] text-cyan-400">
                ENCRYPTION: AES-256
              </span>
            </div>

            {/* Dashboard Content */}
            <div className="grid grid-cols-1 gap-6 p-4 sm:p-7 lg:grid-cols-2">
              <Passport />
              <Leaderboard />
            </div>

            {/* System Status */}
            <div className="grid grid-cols-1 gap-5 border-t border-white/10 bg-white/[0.015] p-6 sm:grid-cols-3">

              {[
                {
                  label: "NETWORK STATUS",
                  value: "CONNECTED",
                  icon: Globe,
                  color: "#00FF66",
                },
                {
                  label: "AI ENGINE",
                  value: "OPERATIONAL",
                  icon: BrainCircuit,
                  color: "#00F0FF",
                },
                {
                  label: "BLOCKCHAIN SYNC",
                  value: "SYNCHRONIZED",
                  icon: Database,
                  color: "#B026FF",
                },
              ].map((item) => (
                <div key={item.label} className="flex items-center gap-3">
                  <item.icon size={18} style={{ color: item.color }} />

                  <div>
                    <p className="font-mono text-[9px] text-zinc-600">
                      {item.label}
                    </p>

                    <p
                      className="mt-1 font-mono text-[10px]"
                      style={{ color: item.color }}
                    >
                      {item.value}
                    </p>
                  </div>
                </div>
              ))}
            </div>
          </div>
          </Reveal>
        </div>

      </section>

      {/* FINAL CTA */}
      <section className="relative z-10 px-5 pb-24">
        <Reveal>
        <div className="relative mx-auto max-w-6xl overflow-hidden rounded-2xl border border-cyan-400/20 bg-[#071116] px-6 py-16 text-center sm:px-12 sm:py-24">

          <div className="absolute left-1/2 top-1/2 h-80 w-80 -translate-x-1/2 -translate-y-1/2 rounded-full bg-cyan-400/10 blur-[100px]" />

          <div className="relative">
            <Sparkles className="mx-auto mb-6 text-cyan-400" size={30} />

            <p className="font-mono text-[10px] tracking-[.3em] text-cyan-400">
              THE NEXT GENERATION STARTS HERE
            </p>

            <h2 className="mt-5 text-4xl font-black sm:text-6xl">
              Stop claiming.{" "}
              <span className="gradient-text">Start proving.</span>
            </h2>

            <p className="mx-auto mt-5 max-w-xl text-sm leading-7 text-zinc-400">
              The future belongs to developers who can prove what they build.
              Your next level is one initialization away.
            </p>

            <a
              href="#access"
              className="mt-9 inline-flex items-center gap-3 rounded-lg bg-cyan-400 px-7 py-4 font-mono text-xs font-black text-black transition hover:scale-105 hover:bg-white"
            >
              INITIALIZE YOUR ACCESS
              <ArrowRight size={17} />
            </a>
          </div>
        </div>
        </Reveal>
      </section>

      {/* FOOTER */}
      <footer className="relative z-10 border-t border-white/10 bg-black px-5 py-8 lg:px-8">

        <div className="mx-auto flex max-w-7xl flex-col justify-between gap-5 sm:flex-row sm:items-center">

          <div className="flex items-center gap-2 font-black">
            <Code2 className="text-cyan-400" />
            SKILL<span className="text-cyan-400">BRIDGE</span>
          </div>

          <div className="flex flex-wrap gap-4 font-mono text-[9px] text-zinc-500">
            <span className="text-green-400">
              ● SYSTEM STATUS: ONLINE
            </span>

            <span>VERSION: v0.1.0-alpha</span>

            <span>© 2026 SKILLBRIDGE.</span>
          </div>
        </div>

        <div className="mx-auto mt-6 max-w-7xl border-t border-white/5 pt-5 font-mono text-[9px] tracking-widest text-zinc-700">
          END_OF_TRANSMISSION // ALL RIGHTS RESERVED
        </div>

      </footer>
    </main>
  );
}