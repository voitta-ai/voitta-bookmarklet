// Aggregates every stylesheet shipped into the closed shadow root.
//
// Order matters: tokens first, then theme palettes (light is the
// default; dark only takes effect when ``data-theme="dark"`` or auto +
// prefers-color-scheme:dark), then components.
//
// widget.tsx concatenates ``cssText`` into a single <style> node — one
// node keeps the shadow-DOM cascade clean and is faster than N styles.

import tokens from "./themes/tokens.css?inline";
import light from "./themes/light.css?inline";
import dark from "./themes/dark.css?inline";

import layout from "./components/layout.css?inline";
import header from "./components/header.css?inline";
import messages from "./components/messages.css?inline";
import composer from "./components/composer.css?inline";
import settings from "./components/settings.css?inline";
import mcpSettings from "./components/mcp-settings.css?inline";
import logs from "./components/logs.css?inline";
import artifacts from "./components/artifacts.css?inline";
import reports from "./components/reports.css?inline";
import plots from "./components/plots.css?inline";
import markdown from "./components/markdown.css?inline";
import report from "./components/report.css?inline";
import workspace from "./components/workspace.css?inline";
import tokenModal from "./components/token-modal.css?inline";
import questionCard from "./components/question-card.css?inline";
import routines from "./components/routines.css?inline";

export const cssText = [
  tokens,
  light,
  dark,
  layout,
  header,
  messages,
  composer,
  settings,
  mcpSettings,
  logs,
  artifacts,
  reports,
  plots,
  markdown,
  report,
  workspace,
  tokenModal,
  questionCard,
  routines,
].join("\n\n");
