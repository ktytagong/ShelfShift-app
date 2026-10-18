import { createContext } from "react";

// True inside the Decision board's guided move drawer (06.1-CONTEXT D-69): a step write there refreshes in place, so
// the board stays open. Everywhere else (false) the stock decision page shows its current step after a write.
export const KeepBoardLocation = createContext(false);
