import * as React from "react"
import { Input as InputPrimitive } from "@base-ui/react/input"

import { cn } from "@/lib/utils"

function Input({ className, type, ...props }: React.ComponentProps<"input">) {
  return (
    <InputPrimitive
      type={type}
      data-slot="input"
      className={cn(
        "h-9 w-full min-w-0 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm text-slate-900 transition-all duration-150 outline-none file:inline-flex file:h-6 file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-foreground placeholder:text-slate-400 hover:border-slate-300 focus-visible:border-[var(--color-brand-primary)]/40 focus-visible:ring-2 focus-visible:ring-[var(--color-brand-primary)]/15 disabled:pointer-events-none disabled:cursor-not-allowed disabled:bg-slate-50 disabled:opacity-60 aria-invalid:border-destructive aria-invalid:ring-2 aria-invalid:ring-destructive/15 dark:border-white/15 dark:bg-white/5 dark:text-slate-100 dark:placeholder:text-slate-500 dark:hover:border-white/25 dark:focus-visible:border-[var(--color-brand-primary)]/60 dark:focus-visible:ring-[var(--color-brand-primary)]/25 dark:disabled:bg-white/[0.02]",
        className
      )}
      {...props}
    />
  )
}

export { Input }
