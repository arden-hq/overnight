export const isTyping = (e: KeyboardEvent): boolean => !!(e.target as HTMLElement).closest("input, select, textarea, [role=combobox], [contenteditable]");

export const isPlainKey = (e: KeyboardEvent): boolean => !e.metaKey && !e.ctrlKey && !e.altKey && !isTyping(e);
