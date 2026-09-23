export const basename = (p: string) => p.split(/[\\/]/).filter(Boolean).pop() ?? p;

export const DEFAULT_PROJECT = "default project";
