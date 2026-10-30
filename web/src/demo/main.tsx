import React from "react";
import ReactDOM from "react-dom/client";
import App from "../App";
import { installDemoApi } from "./mockApi";
import "../demo-base.css";
import "../styles.css";

installDemoApi();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
