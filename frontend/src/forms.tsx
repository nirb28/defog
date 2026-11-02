import { useMemo, type InputHTMLAttributes, type ReactNode } from "react";
import { YEARS } from "./api";

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: ReactNode;
  children: ReactNode;
}) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}

export function TextInput(props: InputHTMLAttributes<HTMLInputElement>) {
  return <input className="input" {...props} />;
}

export function detectTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

const FALLBACK_ZONES = [
  "America/New_York",
  "America/Chicago",
  "America/Denver",
  "America/Phoenix",
  "America/Los_Angeles",
  "America/Anchorage",
  "Pacific/Honolulu",
  "Europe/London",
  "Europe/Paris",
  "Asia/Kolkata",
  "Asia/Shanghai",
  "Asia/Tokyo",
  "Australia/Sydney",
  "UTC",
];

export function TimeZoneSelect({ value, onChange }: { value: string; onChange: (tz: string) => void }) {
  const zones = useMemo(() => {
    let list: string[] = FALLBACK_ZONES;
    try {
      list = Intl.supportedValuesOf("timeZone");
    } catch {
      // Older browsers: keep the short list.
    }
    return list.includes(value) ? list : [value, ...list];
  }, [value]);
  return (
    <select className="input" value={value} onChange={(e) => onChange(e.target.value)} required>
      {zones.map((z) => (
        <option key={z} value={z}>
          {z.replace(/_/g, " ")}
        </option>
      ))}
    </select>
  );
}

export function YearSelect({ value, onChange }: { value: string; onChange: (year: string) => void }) {
  return (
    <select className="input" value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">Prefer not to say</option>
      {YEARS.map((y) => (
        <option key={y} value={y}>
          {y}
        </option>
      ))}
    </select>
  );
}

export const SCHOOLS = [
  "University of Pennsylvania",
  "Drexel University",
  "Temple University",
  "Villanova University",
  "Swarthmore College",
  "Haverford College",
  "Bryn Mawr College",
  "Penn State University",
];

export function SchoolInput({ value, onChange }: { value: string; onChange: (s: string) => void }) {
  return (
    <>
      <input
        className="input"
        list="schools"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        maxLength={200}
        required
        autoComplete="organization"
        placeholder="Start typing your school"
      />
      <datalist id="schools">
        {SCHOOLS.map((s) => (
          <option key={s} value={s} />
        ))}
      </datalist>
    </>
  );
}

export function FormError({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <p className="form-error" role="alert">
      {message}
    </p>
  );
}

export function errorText(err: unknown): string {
  return err instanceof Error ? err.message : "Something went wrong. Try again.";
}
