 "use client";

import { useEffect, useState } from "react";
import {
  ArrowRight,
  CheckCircle2,
  LoaderCircle,
  Lock,
  Mail,
  ShieldCheck,
} from "lucide-react";

type VerifyApiResponse = {
  verified?: boolean;
  reference_code?: string;
  vip_number?: number | null;
  message?: string;
  detail?: string;
};

type ResendApiResponse = {
  message?: string;
  detail?: string;
};

type Props = {
  initialToken: string;
};

const RESEND_COOLDOWN_SECONDS = 300;

export default function VerifyPanel({ initialToken }: Props) {
  const [verificationState, setVerificationState] = useState<
    "idle" | "verifying" | "verified"
  >("idle");

  const [verificationError, setVerificationError] = useState("");
  const [verificationResult, setVerificationResult] =
    useState<VerifyApiResponse | null>(null);

  const [email, setEmail] = useState("");
  const [resending, setResending] = useState(false);
  const [resendMessage, setResendMessage] = useState("");
  const [resendError, setResendError] = useState("");
  const [cooldown, setCooldown] = useState(0);

  const hasToken = initialToken.trim().length > 0;
  const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/+$/, "");

  useEffect(() => {
    if (cooldown <= 0) return;

    const timer = window.setTimeout(() => {
      setCooldown((current) => Math.max(0, current - 1));
    }, 1000);

    return () => window.clearTimeout(timer);
  }, [cooldown]);

  async function confirmVerification() {
    if (!hasToken || verificationState === "verifying" || !apiBase) {
      if (!apiBase) {
        setVerificationError("Verification service configuration is missing.");
      }
      return;
    }

    setVerificationError("");
    setVerificationState("verifying");

    try {
      const response = await fetch(`${apiBase}/api/v1/waitlist/verify`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ token: initialToken }),
      });

      const data = (await response.json().catch(() => null)) as
        | VerifyApiResponse
        | null;

      if (response.status === 200 && data?.verified === true) {
        setVerificationResult(data);
        setVerificationState("verified");
        return;
      }

      switch (response.status) {
        case 400:
          setVerificationError(
            "This verification link is invalid, expired, or has already been used. Request a new verification email below."
          );
          break;

        case 422:
          setVerificationError("The verification token format is invalid.");
          break;

        case 429:
          setVerificationError("Too many verification attempts. Please try again later.");
          break;

        case 503:
          setVerificationError(
            "Verification is temporarily unavailable. Please try again shortly."
          );
          break;

        default:
          setVerificationError(
            `Verification failed (HTTP ${response.status}). Please try again.`
          );
      }

      setVerificationState("idle");
    } catch {
      setVerificationError(
        "Could not connect to SkillBridge. Check your connection and try again."
      );
      setVerificationState("idle");
    }
  }

  async function requestNewEmail(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setResendError("");
    setResendMessage("");

    if (!apiBase) {
      setResendError("Resend service configuration is missing.");
      return;
    }

    if (cooldown > 0 || resending) return;

    setResending(true);

    try {
      const response = await fetch(
        `${apiBase}/api/v1/waitlist/resend-verification`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ email: email.trim() }),
        }
      );

      const data = (await response.json().catch(() => null)) as
        | ResendApiResponse
        | null;

      if (response.status === 202) {
        setResendMessage(
          typeof data?.message === "string"
            ? data.message
            : "If an eligible waitlist entry exists, a verification email will be sent shortly."
        );
        setCooldown(RESEND_COOLDOWN_SECONDS);
        return;
      }

      if (response.status === 422) {
        setResendError("Please enter a valid email address.");
      } else if (response.status === 429) {
        const retryAfter = Number(response.headers.get("Retry-After"));
        setCooldown(
          Number.isFinite(retryAfter) && retryAfter > 0
            ? retryAfter
            : RESEND_COOLDOWN_SECONDS
        );
        setResendError("Please wait before requesting another verification email.");
      } else if (response.status === 503) {
        setResendError("Resend service is temporarily unavailable.");
      } else {
        setResendError(`Request failed (HTTP ${response.status}).`);
      }
    } catch {
      setResendError(
        "Could not connect to SkillBridge. Please check your connection."
      );
    } finally {
      setResending(false);
    }
  }

  const formattedCooldown = `${Math.floor(cooldown / 60)
    .toString()
    .padStart(2, "0")}:${(cooldown % 60).toString().padStart(2, "0")}`;

  return (
    <div className="mx-auto w-full max-w-2xl">
      <div className="glass-card rounded-2xl p-6 sm:p-9">
        <div className="mb-8 flex items-center gap-3">
          <span className="flex h-11 w-11 items-center justify-center rounded-lg bg-cyan-400/10 text-cyan-400">
            <ShieldCheck size={23} />
          </span>
          <div>
            <p className="font-mono text-xs tracking-[.2em] text-cyan-400">
              SKILLBRIDGE // IDENTITY
            </p>
            <p className="mt-1 text-xs text-zinc-500">
              EMAIL VERIFICATION PROTOCOL
            </p>
          </div>
        </div>

        {verificationState === "verified" && verificationResult ? (
          <div role="status" aria-live="polite">
            <CheckCircle2 size={38} className="text-green-400" />

            <h1 className="mt-5 text-3xl font-black">
              Email <span className="text-green-400">verified.</span>
            </h1>

            <p className="mt-4 text-sm leading-7 text-zinc-400">
              {verificationResult.message}
            </p>

            <div className="mt-7 space-y-4 rounded-xl border border-green-400/20 bg-green-400/5 p-5">
              <div>
                <p className="font-mono text-[10px] tracking-widest text-zinc-500">
                  YOUR REFERENCE CODE
                </p>
                <p className="mt-2 break-all font-mono text-lg text-cyan-300">
                  {verificationResult.reference_code}
                </p>
              </div>

              <div className="border-t border-white/10 pt-4">
                <p className="font-mono text-[10px] tracking-widest text-zinc-500">
                  VIP EARLY ACCESS
                </p>
                <p className="mt-2 font-mono text-2xl font-bold text-green-400">
                  {verificationResult.vip_number == null
                    ? "CAPACITY FULL"
                    : `#${verificationResult.vip_number}`}
                </p>
              </div>
            </div>
          </div>
        ) : (
          <>
            <h1 className="text-3xl font-black sm:text-4xl">
              Confirm your <span className="gradient-text">identity.</span>
            </h1>

            <p className="mt-4 text-sm leading-7 text-zinc-400">
              Complete the email verification step to confirm your SkillBridge
              waitlist registration.
            </p>

            {hasToken ? (
              <div className="mt-8 rounded-xl border border-cyan-400/20 bg-cyan-400/5 p-5">
                <p className="font-mono text-[10px] tracking-widest text-cyan-400">
                  VERIFICATION TOKEN DETECTED
                </p>

                <p className="mt-3 text-xs leading-6 text-zinc-400">
                  Your verification link is ready. Press the button below to
                  confirm your email. Opening this page alone will not consume
                  your token.
                </p>

                <button
                  type="button"
                  onClick={confirmVerification}
                  disabled={verificationState === "verifying"}
                  className="mt-6 flex w-full items-center justify-center gap-3 rounded-lg bg-cyan-400 px-6 py-4 font-mono text-xs font-black text-black transition hover:bg-white disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {verificationState === "verifying" ? (
                    <>
                      <LoaderCircle size={16} className="animate-spin" />
                      VERIFYING...
                    </>
                  ) : (
                    <>
                      CONFIRM EMAIL
                      <ArrowRight size={16} />
                    </>
                  )}
                </button>
              </div>
            ) : (
              <div className="mt-8 rounded-xl border border-amber-400/20 bg-amber-400/5 p-5">
                <p className="font-mono text-xs text-amber-300">
                  VERIFICATION TOKEN NOT FOUND
                </p>
                <p className="mt-2 text-xs leading-6 text-zinc-400">
                  Open the complete verification link received in your email,
                  or request a new verification email below.
                </p>
              </div>
            )}

            {verificationError && (
              <p role="alert" className="mt-5 text-xs leading-6 text-red-400">
                {verificationError}
              </p>
            )}
          </>
        )}

        {verificationState !== "verified" && (
          <div className="mt-9 border-t border-white/10 pt-7">
            <div className="flex items-center gap-2">
              <Mail size={16} className="text-purple-400" />
              <h2 className="font-mono text-xs tracking-widest text-purple-300">
                DIDN&apos;T RECEIVE YOUR EMAIL?
              </h2>
            </div>

            <p className="mt-3 text-xs leading-6 text-zinc-500">
              Request another verification message using the email address
              you registered with.
            </p>

            <form onSubmit={requestNewEmail} className="mt-5 space-y-3">
              <input
                type="email"
                required
                maxLength={320}
                autoComplete="email"
                value={email}
                onChange={(event) => {
                  setEmail(event.target.value);
                  setResendError("");
                }}
                placeholder="enter_registered_email..."
                aria-label="Registered email address"
                disabled={resending || cooldown > 0}
                className="w-full rounded-lg border border-white/10 bg-black/40 px-4 py-4 font-mono text-xs text-white outline-none placeholder:text-zinc-600 focus:border-purple-400/60 disabled:opacity-60"
              />

              <button
                type="submit"
                disabled={resending || cooldown > 0}
                className="flex w-full items-center justify-center gap-2 rounded-lg border border-purple-400/30 bg-purple-400/10 px-5 py-4 font-mono text-xs font-bold text-purple-200 transition hover:bg-purple-400/20 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {resending ? (
                  <>
                    <LoaderCircle size={15} className="animate-spin" />
                    REQUESTING...
                  </>
                ) : cooldown > 0 ? (
                  `RETRY AVAILABLE IN ${formattedCooldown}`
                ) : (
                  <>
                    RESEND VERIFICATION
                    <ArrowRight size={15} />
                  </>
                )}
              </button>
            </form>

            {resendMessage && (
              <p
                role="status"
                aria-live="polite"
                className="mt-4 text-xs leading-6 text-green-300"
              >
                {resendMessage}
              </p>
            )}

            {resendError && (
              <p role="alert" className="mt-4 text-xs leading-6 text-red-400">
                {resendError}
              </p>
            )}

            {process.env.NEXT_PUBLIC_EMAIL_MODE === "console" && (
              <p className="mt-5 text-[11px] leading-6 text-amber-300">
                DEVELOPMENT MODE: Email delivery is disabled. Resend requests
                can be tested, but no email will reach an inbox yet.
              </p>
            )}
          </div>
        )}
      </div>

      <p className="mt-5 flex items-center justify-center gap-2 font-mono text-[9px] tracking-widest text-zinc-600">
        <Lock size={11} />
        SECURE VERIFICATION // SKILLBRIDGE
      </p>
    </div>
  );
}
