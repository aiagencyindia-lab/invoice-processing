"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { DimensionMeter, percent } from "@/components/trust/DimensionMeter";
import { api, ApiError, can, type Me, type VendorDocumentList, type VendorList, type VendorTrustProfile } from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { DIMENSION_LABELS } from "@/lib/trust";

const SUBKINDS: Record<string, string> = {
  gst_certificate: "GST certificate",
  pan: "PAN",
  udyam: "Udyam",
  cancelled_cheque: "Cancelled cheque",
  abn: "ABN",
};

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="tile">
      <span className="tile-label">{label}</span>
      <span className="tile-value">{value}</span>
    </div>
  );
}

export default function VendorPage() {
  const { id } = useParams<{ id: string }>();
  // Keyed so one vendor's profile never shows under another's name.
  return <VendorView key={id} id={id} />;
}

function VendorView({ id }: { id: string }) {
  const { data: me } = usePoll<Me>("/api/me");
  const { data: list, error: listError } = usePoll<VendorList>("/api/vendors");
  const vendor = list?.vendors.find((row) => row.id === id) ?? null;
  // Both answer 503 until the score and provenance branches ship; the page
  // still shows the vendor-master facts and says what is missing.
  const { data: profile, error: profileError } = usePoll<VendorTrustProfile>(
    vendor?.node ? `/api/trust/vendors/${encodeURIComponent(vendor.node)}/trust` : null,
  );
  const docsPath = `/api/vendors/${encodeURIComponent(id)}/documents`;
  const { data: docs, error: docsError, reload: reloadDocs } = usePoll<VendorDocumentList>(
    vendor ? docsPath : null,
  );
  const [subkind, setSubkind] = useState("gst_certificate");
  const [notice, setNotice] = useState<string | null>(null);

  if (listError) return <div className="empty-state">{listError}</div>;
  if (!list) return <div className="empty-state">Loading…</div>;
  if (!vendor) {
    return (
      <section className="stack" style={{ gap: 16 }}>
        <Link href="/vendors" style={{ fontSize: 12.5 }}>
          ‹ Vendors
        </Link>
        <h1>Vendor</h1>
        <div className="empty-state">No such vendor.</div>
      </section>
    );
  }

  async function upload(file: File) {
    setNotice(null);
    const form = new FormData();
    form.append("file", file);
    form.append("subkind", subkind);
    try {
      await api(docsPath, { method: "POST", body: form });
      setNotice(`Uploaded ${file.name}; its authenticity is being scored.`);
      reloadDocs();
    } catch (caught) {
      setNotice(caught instanceof ApiError ? caught.friendly : String(caught));
    }
  }

  const gst = profile?.gst;
  const types = Object.entries(profile?.findings_by_type ?? {});

  return (
    <section className="stack" style={{ gap: 16 }}>
      <Link href="/vendors" style={{ fontSize: 12.5 }}>
        ‹ Vendors
      </Link>
      <h1>{vendor.name}</h1>

      <div className="tiles" role="region" aria-label="Vendor profile">
        <Fact label="Tax ID" value={vendor.tax_id || "—"} />
        <Fact label="Kind" value={vendor.kind || "—"} />
        <Fact label="Bank" value={vendor.bank_status} />
        <Fact
          label="GST standing"
          value={gst?.status ? `${gst.status}${gst.missed_periods ? ` · ${gst.missed_periods} missed` : ""}` : "—"}
        />
      </div>

      {!vendor.node ? (
        <div className="empty-state">This vendor is not in the evidence graph yet, so it has no trust profile.</div>
      ) : profileError ? (
        <div className="empty-state">{profileError}</div>
      ) : !profile ? (
        <div className="empty-state">Loading the trust profile…</div>
      ) : (
        <>
          <div className="panel stack" style={{ padding: 16, gap: 10 }} role="region" aria-label="Trust dimensions">
            <div className="eyebrow">Trust dimensions · mean of recent invoices</div>
            {Object.keys(DIMENSION_LABELS).map((name) => (
              <DimensionMeter
                key={name}
                name={name}
                score={profile.dimensions[name as keyof typeof profile.dimensions] ?? null}
              />
            ))}
          </div>

          <div className="panel stack" style={{ padding: 16, gap: 8 }}>
            <div className="eyebrow">Findings by type</div>
            {types.length ? (
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Type</th>
                    <th>Open</th>
                    <th>Decided</th>
                  </tr>
                </thead>
                <tbody>
                  {types.map(([type, counts]) => (
                    <tr key={type}>
                      <td>
                        <Link href={`/findings?type=${encodeURIComponent(type)}&vendor=${encodeURIComponent(vendor.node ?? "")}`}>
                          {type}
                        </Link>
                      </td>
                      <td>{counts.open}</td>
                      <td>{counts.decided}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <span className="muted" style={{ fontSize: 12.5 }}>
                No findings against this vendor.
              </span>
            )}
          </div>

          <div className="panel stack" style={{ padding: 16, gap: 8 }}>
            <div className="eyebrow">Network</div>
            {profile.network.length ? (
              <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
                {profile.network.map((link) => (
                  <li key={link.vendor.node}>
                    {link.vendor.name ?? link.vendor.node} · shares {link.shared.join(", ")}
                  </li>
                ))}
              </ul>
            ) : (
              <span className="muted" style={{ fontSize: 12.5 }}>
                No other vendor shares a bank account, tax ID or address with this one.
              </span>
            )}
          </div>

          <div className="panel stack" style={{ padding: 16, gap: 6 }} role="region" aria-label="Monthly composite">
            <div className="eyebrow">Monthly composite · advisory</div>
            {profile.timeline.length ? (
              profile.timeline.map((point) => (
                <div key={point.month} className="row" style={{ gap: 8, alignItems: "center", fontSize: 12.5 }}>
                  <span style={{ width: 64, fontVariantNumeric: "tabular-nums" }}>{point.month}</span>
                  <span style={{ flex: 1, background: "var(--surface-alt)", height: 8, borderRadius: 4 }}>
                    <span
                      style={{
                        display: "block",
                        height: 8,
                        borderRadius: 4,
                        background: "var(--ink)",
                        width: `${Math.round((point.composite ?? 0) * 100)}%`,
                      }}
                    />
                  </span>
                  <span style={{ width: 40, textAlign: "right" }}>{percent(point.composite)}</span>
                </div>
              ))
            ) : (
              <span className="muted" style={{ fontSize: 12.5 }}>
                No scored invoices yet.
              </span>
            )}
          </div>
        </>
      )}

      <div className="panel stack" style={{ padding: 16, gap: 8 }} role="region" aria-label="KYC documents">
        <div className="eyebrow">KYC documents</div>
        {docsError ? (
          <span className="muted" style={{ fontSize: 12.5 }}>
            {docsError}
          </span>
        ) : docs?.documents.length ? (
          <table className="data-table">
            <thead>
              <tr>
                <th>Document</th>
                <th>File</th>
                <th>Authenticity</th>
                <th>Uploaded</th>
              </tr>
            </thead>
            <tbody>
              {docs.documents.map((doc) => (
                <tr key={doc.document_id}>
                  <td>{SUBKINDS[doc.subkind] ?? doc.subkind}</td>
                  <td>{doc.filename ?? "—"}</td>
                  <td>{doc.score === null ? "scoring…" : percent(doc.score)}</td>
                  <td>{doc.uploaded_at ? new Date(doc.uploaded_at).toLocaleDateString() : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : docs ? (
          <span className="muted" style={{ fontSize: 12.5 }}>
            None on file.
          </span>
        ) : null}
        {can(me, "clerk") ? (
          <div className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <select
              className="input-field"
              aria-label="Document kind"
              value={subkind}
              onChange={(event) => setSubkind(event.target.value)}
              style={{ width: "auto" }}
            >
              {Object.entries(SUBKINDS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
            <input
              type="file"
              aria-label="Upload KYC document"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) void upload(file);
                event.target.value = "";
              }}
            />
          </div>
        ) : null}
      </div>

      {notice ? (
        <div className="toast" role="status">
          {notice}
        </div>
      ) : null}
    </section>
  );
}
