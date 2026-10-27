/** Presentation helpers. Formatting only -- no value is ever invented here:
 *  a missing number renders as an em dash, not as zero. */

export const DASH = "—";

export function num(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  return value.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function decimal(value: number | null | undefined, digits = 2): string {
  return num(value, digits);
}

export function money(value: number | null | undefined, currency = "USD"): string {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  try {
    return value.toLocaleString("en-US", {
      style: "currency", currency: currency || "USD",
      minimumFractionDigits: Math.abs(value) < 1 ? 4 : 2,
      maximumFractionDigits: Math.abs(value) < 1 ? 6 : 2,
    });
  } catch {
    return `${currency} ${decimal(value)}`;
  }
}

export function percent(share: number | null | undefined, digits = 1): string {
  if (share === null || share === undefined || Number.isNaN(share)) return DASH;
  return `${(share * 100).toFixed(digits)}%`;
}

export function bytes(size: number | null | undefined): string {
  if (!size && size !== 0) return DASH;
  const units = ["B", "KB", "MB", "GB"];
  let v = size;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${i === 0 ? v : v.toFixed(1)} ${units[i]}`;
}

export function duration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return DASH;
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  const mins = Math.floor(ms / 60_000);
  return `${mins}m ${Math.round((ms % 60_000) / 1000)}s`;
}

export function when(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-GB", {
    year: "numeric", month: "short", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
  });
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const secs = (Date.now() - d.getTime()) / 1000;
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)} min ago`;
  if (secs < 86_400) return `${Math.floor(secs / 3600)} h ago`;
  if (secs < 604_800) return `${Math.floor(secs / 86_400)} d ago`;
  return when(iso);
}

export function titleCase(s: string): string {
  return s.replace(/[_.]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Short label for a snake_case enum value. */
export function label(s: string | null | undefined): string {
  if (!s) return DASH;
  return s.replace(/_/g, " ");
}
