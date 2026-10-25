import { defineRailway, github, postgres, project, service, volume } from "railway/iac";

export default defineRailway(() => {
  const Postgres = postgres("Postgres", { region: "sfo" });
  Postgres.networking = { privateNetworkEndpoint: "postgres" };
  const postgresVolume = volume("postgres-volume", { alerts: { usage: { "100": {}, "80": {}, "95": {} } }, allowOnlineResize: true, region: "sfo", sizeMB: 5000 });
  const ledgerlyVolume = volume("ledgerly-volume", { alerts: { usage: { "100": {}, "80": {}, "95": {} } }, allowOnlineResize: true, region: "sfo", sizeMB: 5000 });
  const invoiceProcessing = service("invoice-processing", {
    source: github("aiagencyindia-lab/invoice-processing", { branch: "main", checkSuites: false }),
    start: "sh scripts/start-railway.sh",
    healthcheck: "/healthz",
    preDeploy: "python -m invoice_processing.db.migrate",
    replicas: { "sfo": 1 },
    deploy: { drainingSeconds: 60 },
    volumeMounts: { "/data": ledgerlyVolume },
  });
  const app = service("app", {
    source: github("aiagencyindia-lab/invoice-processing", { branch: "main", checkSuites: false, rootDirectory: "frontend" }),
    replicas: { "sfo": 1 },
  });

  return project("ledgerly-new", {
    variables: { managed: false },
    resources: [Postgres, invoiceProcessing, app, postgresVolume, ledgerlyVolume],
  });
});
