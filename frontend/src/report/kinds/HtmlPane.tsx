// HTML report iframe pane. The only report kind.
//
// The server pre-renders HTML and the FE just mounts an iframe at the
// URL — a fully pre-rasterised Jinja-rendered document from
// /api/html-report.
//
// The iframe geometry only matters for the screenshot path (where the
// existing primitive resizes it
// to a desktop-class width before capture).

import { useMemo } from "react";
import { useSetRecoilState } from "recoil";
import { reportLoadingState } from "../state";
import type { HtmlPayload } from "../types";

interface Props {
  backendOrigin: string;
  reportName: string;
  renderId: string;
  payload: HtmlPayload;
}

export default function HtmlPane({ backendOrigin, payload }: Props) {
  const setLoading = useSetRecoilState(reportLoadingState);
  const src = useMemo(() => {
    let url: string;
    try {
      url = new URL(payload.url, backendOrigin).toString();
    } catch {
      url = backendOrigin + payload.url;
    }
    // Under the Voitta Chrome extension, frame its relay page instead: a page
    // whose CSP has no frame-src for the backend blocks the report's iframe,
    // but not an extension's page (extension/src/report-frame.js).
    const reportFrame = (window as unknown as { __voittaExt?: { reportFrame?: string } })
      .__voittaExt?.reportFrame;
    return reportFrame ? `${reportFrame}?u=${encodeURIComponent(url)}` : url;
  }, [backendOrigin, payload.url]);

  return (
    <iframe
      src={src}
      title={payload.title || payload.url}
      className="report-panel-iframe"
      style={{
        width: "100%",
        height: "100%",
        border: 0,
        display: "block",
        background: "var(--voitta-bg, transparent)",
      }}
      onLoad={() => setLoading(false)}
    />
  );
}
