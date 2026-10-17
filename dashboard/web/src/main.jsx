import React from "react";
import { createRoot } from "react-dom/client";
import Root from "./Root.jsx";
import "./styles.css";
import "./theme.css"; // after styles.css so the paper palette wins

createRoot(document.getElementById("root")).render(<Root />);
