import { useState, type FormEvent } from "react";
import { api, apiHref, type User } from "./api";
import { errorText, Field, FormError, SchoolInput, TextInput, TimeZoneSelect, YearSelect } from "./forms";

export default function SettingsPage({
  user,
  onUser,
  onSignedOut,
}: {
  user: User;
  onUser: (u: User) => void;
  onSignedOut: () => void;
}) {
  const [name, setName] = useState(user.name);
  const [school, setSchool] = useState(user.school ?? "");
  const [year, setYear] = useState<string>(user.year ?? "");
  const [major, setMajor] = useState(user.major ?? "");
  const [timeZone, setTimeZone] = useState(user.time_zone ?? "UTC");
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmEmail, setConfirmEmail] = useState("");
  const [deleteError, setDeleteError] = useState<string | null>(null);

  async function save(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSaved(false);
    try {
      onUser(await api.updateMe({ name, school, year: year || null, major: major.trim() || null, time_zone: timeZone }));
      setSaved(true);
    } catch (err) {
      setError(errorText(err));
    }
  }

  async function signOut(all: boolean) {
    try {
      await (all ? api.logoutAll() : api.logout());
    } finally {
      onSignedOut();
    }
  }

  async function deleteAccount(e: FormEvent) {
    e.preventDefault();
    setDeleteError(null);
    try {
      await api.deleteMe(confirmEmail);
      onSignedOut();
    } catch (err) {
      setDeleteError(errorText(err));
    }
  }

  return (
    <div className="page">
      <div className="page-inner">
        <h1 className="page-title">Account</h1>

        <section className="card">
          <h2 className="card-title">Profile</h2>
          <p className="card-sub">
            {user.email}
            {user.google_linked && " · Google sign-in linked"}
          </p>
          <form className="form" onSubmit={save}>
            <Field label="Full name">
              <TextInput value={name} onChange={(e) => setName(e.target.value)} maxLength={100} required />
            </Field>
            <Field label="School or university">
              <SchoolInput value={school} onChange={setSchool} />
            </Field>
            <div className="field-row">
              <Field label="Year of study">
                <YearSelect value={year} onChange={setYear} />
              </Field>
              <Field label="Major">
                <TextInput value={major} onChange={(e) => setMajor(e.target.value)} maxLength={200} />
              </Field>
            </div>
            <Field label="Time zone">
              <TimeZoneSelect value={timeZone} onChange={setTimeZone} />
            </Field>
            <FormError message={error} />
            {saved && <p className="form-notice">Saved.</p>}
            <button className="primary-button">Save changes</button>
          </form>
        </section>

        <section className="card">
          <h2 className="card-title">Sessions</h2>
          <div className="card-actions">
            <button type="button" className="ghost-button" onClick={() => void signOut(false)}>
              Log out
            </button>
            <button type="button" className="ghost-button" onClick={() => void signOut(true)}>
              Log out of all devices
            </button>
          </div>
        </section>

        <section className="card">
          <h2 className="card-title">Your data</h2>
          <p className="card-sub">
            Download everything Defog stores about you: your profile, synced Canvas and calendar data, and chats.
          </p>
          <div className="card-actions">
            <a className="ghost-button" href={apiHref("/api/me/export")} download>
              Download my data
            </a>
          </div>
        </section>

        <section className="card card-danger">
          <h2 className="card-title">Delete account</h2>
          <p className="card-sub">
            This permanently deletes your account, chats and all synced data, and disconnects Canvas and Google Calendar.
          </p>
          <form className="form" onSubmit={deleteAccount}>
            <Field label={`Type ${user.email} to confirm`}>
              <TextInput value={confirmEmail} onChange={(e) => setConfirmEmail(e.target.value)} autoComplete="off" />
            </Field>
            <FormError message={deleteError} />
            <button className="danger-button" disabled={confirmEmail.trim().toLowerCase() !== user.email}>
              Delete my account
            </button>
          </form>
        </section>
      </div>
    </div>
  );
}
