export const basename = (p: string) => p.split(/[\\/]/).filter(Boolean).pop() ?? p;

/** Display name of the project a session without its own root runs in. */
export const DEFAULT_PROJECT = "默认工作区";
