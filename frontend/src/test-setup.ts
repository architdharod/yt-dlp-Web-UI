import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

/**
 * Testing Library only auto-cleans when a global `afterEach` exists, which
 * needs `globals: true` — so instead the hook is registered explicitly here and
 * the tests keep importing what they use from vitest.
 */
afterEach(cleanup);
