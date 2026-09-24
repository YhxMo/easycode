import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import * as api from "../api";
import { primeApiMock } from "./helpers";

// Switching to a model without a configured API key returns
// HTTP 422 and the old code closed the menu / only logged, so the user believed
// the switch succeeded while still running on the previous model. The
// ModelPicker now reports the failure through `onError` to the App toast AND
// keeps the old model highlighted (onChange is only called on success).
//
// Test drives App purely against a mocked ./api (easycode-audit rule 3: no
// network, no real ~/.easycode).
vi.mock("../api", async () => (await import("./helpers")).apiMock);

const m = vi.mocked(api);

beforeEach(() => {
  primeApiMock(m);
  m.fetchSessions.mockResolvedValue([]);
  // Two models so a switch to a second one can be attempted.
  m.fetchModels.mockResolvedValue({
    default: "alpha",
    models: { alpha: { model: "m-alpha" }, beta: { model: "m-beta" } },
    providers: { alpha: "openai", beta: "openai" },
    limits: {},
  });
});

describe("App · 模型切换失败反馈", () => {
  it("switchModel 拒绝（422）后错误 toast 出现且旧模型保持选中", async () => {
    const user = userEvent.setup();
    m.switchModel.mockRejectedValue(new Error("no API key configured"));

    render(<App />);

    // ModelPicker loads alpha as the active model.
    await screen.findByText("m-alpha");

    // Open the model dropdown.
    const trigger = document.querySelector(".model-trigger") as HTMLElement;
    expect(trigger).toBeTruthy();
    await user.click(trigger);

    // Choose the beta model from the menu.
    await waitFor(() => {
      expect(screen.getByText("m-beta")).toBeTruthy();
    });
    fireEvent.click(screen.getByText("m-beta"));

    // The failure surfaces on the App toast with the err.message.
    await waitFor(() => {
      expect(document.querySelector(".app-toast")?.textContent).toContain("no API key configured");
    });

    // The old model (alpha) stays the active one — the switch must not have
    // moved selection to beta. The trigger's dropdown value still reads m-alpha.
    expect(document.querySelector(".model-trigger .dropdown-value")?.textContent).toBe("m-alpha");
  });
});
