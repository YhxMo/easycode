import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LoadingState } from "../components/primitives/LoadingState";

describe("LoadingState", () => {
  it("显示标签、九格像素网格与已用时间", () => {
    render(<LoadingState label="正在探索" />);
    expect(screen.getByRole("status")).toBeTruthy();
    expect(screen.getByText("正在探索")).toBeTruthy();
    expect(document.querySelectorAll(".loading-grid > span")).toHaveLength(9);
    expect(screen.getByText("0.0s")).toBeTruthy();
  });
});
