(function () {
  "use strict";
  // Aide tab: the supervisor profile's cron jobs, the incidents it holds cards behind, and the
  // reports its runs printed. Data comes from /api/plugins/charm/aide (dashboard/plugin_api.py).
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const h = React.createElement;
  const { useState, useEffect } = SDK.hooks;
  const { Card, CardHeader, CardTitle, CardContent, Badge, Button } = SDK.components;
  const REFRESH_MS = 30000;

  const stack = { display: "flex", flexDirection: "column", gap: "16px" };
  const muted = { opacity: 0.65, fontSize: "12px" };
  const cell = { padding: "6px 10px 6px 0", textAlign: "left", verticalAlign: "top" };
  const pre = { whiteSpace: "pre-wrap", wordBreak: "break-word", fontSize: "12px", margin: 0 };

  // SDK.utils.isoTimeAgo reads every future time as "just now", and next runs are in the future.
  function ago(iso) {
    if (!iso) return "never";
    const sec = Math.round((Date.parse(iso) - Date.now()) / 1000);
    const abs = Math.abs(sec);
    const span = abs < 60 ? abs + "s" : abs < 3600 ? Math.floor(abs / 60) + "m"
      : abs < 86400 ? Math.floor(abs / 3600) + "h" : Math.floor(abs / 86400) + "d";
    return sec >= 0 ? "in " + span : span + " ago";
  }
  // Output files are named YYYY-MM-DD_HH-MM-SS in local time.
  const runTime = (stem) => stem.replace("_", " ").replace(/-(\d\d)-(\d\d)$/, ":$1:$2");

  function health(job) {
    if (job.state === "paused") return h(Badge, { variant: "outline" }, "paused");
    if (job.last_status && job.last_status !== "ok") return h(Badge, { variant: "destructive" }, job.last_status);
    if (!job.last_run_at) return h(Badge, { variant: "outline" }, "not run yet");
    return h(Badge, { variant: "secondary" }, "ok");
  }

  function Jobs({ jobs }) {
    const head = ["Job", "Schedule", "Health", "Last run", "Next run", "Delivers to"];
    return h(Card, null,
      h(CardHeader, null, h(CardTitle, null, "Jobs")),
      h(CardContent, { style: { overflowX: "auto" } },
        h("table", { style: { width: "100%", borderCollapse: "collapse", fontSize: "13px" } },
          h("thead", null, h("tr", null, head.map((t) => h("th", { key: t, style: { ...cell, ...muted } }, t)))),
          h("tbody", null, jobs.map((job) =>
            h("tr", { key: job.id },
              h("td", { style: cell }, job.name,
                job.last_error && h("div", { style: { ...muted, color: "var(--destructive, #c33)" } }, job.last_error)),
              h("td", { style: cell }, job.schedule_display),
              h("td", { style: cell }, health(job),
                job.failure_streak > 0 && h("span", { style: muted }, " ×" + job.failure_streak)),
              h("td", { style: cell }, ago(job.last_run_at)),
              h("td", { style: cell }, job.state === "paused" ? "—" : ago(job.next_run_at)),
              h("td", { style: cell }, job.deliver)))))));
  }

  function Incidents({ incidents }) {
    return h(Card, null,
      h(CardHeader, null, h(CardTitle, null, "Open incidents")),
      h(CardContent, { style: stack },
        incidents.length === 0 && h("div", { style: muted }, "No incident is holding cards."),
        incidents.map((inc) =>
          h("div", { key: inc.board + inc.card },
            h("div", { style: { fontWeight: 600 } }, inc.title || inc.card),
            h("div", { style: muted },
              inc.board + " · " + inc.card + " · " + inc.status + " · " + inc.held + " of " + inc.members +
                " cards held · opened " + SDK.utils.timeAgo(inc.opened_at) +
                " · complete the card to release them"),
            h("p", { style: { ...pre, marginTop: "6px" } }, inc.sample)))));
  }

  function Activity({ runs }) {
    return h(Card, null,
      h(CardHeader, null, h(CardTitle, null, "Recent reports")),
      h(CardContent, { style: stack },
        runs.length === 0 && h("div", { style: muted }, "No run has reported anything yet. Silent runs are left out."),
        runs.map((run) =>
          h("div", { key: run.job + run.at },
            h("div", { style: muted }, run.job + " · " + runTime(run.at)),
            h("pre", { style: pre }, run.body)))));
  }

  function AidePage() {
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);
    const load = () => SDK.fetchJSON("/api/plugins/charm/aide")
      .then((d) => { setData(d); setError(null); })
      .catch((e) => setError(String(e && e.message || e)));
    useEffect(() => {
      load();
      const timer = setInterval(load, REFRESH_MS);
      return () => clearInterval(timer);
    }, []);

    return h("div", { style: { ...stack, padding: "16px", maxWidth: "1100px" } },
      h("div", { style: { display: "flex", alignItems: "center", justifyContent: "space-between" } },
        h("div", null,
          h("h1", { style: { fontSize: "20px", fontWeight: 600, margin: 0 } }, "Aide"),
          h("div", { style: muted }, "Supervises the kanban fleet. Refreshes every 30 seconds.")),
        h(Button, { variant: "outline", size: "sm", onClick: load }, "Refresh")),
      error && h("div", { style: { color: "var(--destructive, #c33)" } }, "Could not load Aide's state: " + error),
      data && h(Incidents, { incidents: data.incidents }),
      data && h(Jobs, { jobs: data.jobs }),
      data && h(Activity, { runs: data.activity }));
  }

  window.__HERMES_PLUGINS__.register("charm", AidePage);
})();
