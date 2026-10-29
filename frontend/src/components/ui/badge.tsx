import { mergeProps } from "@base-ui/react/merge-props"
import { useRender } from "@base-ui/react/use-render"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

const badgeVariants = cva(
  "group/badge inline-flex h-5 w-fit shrink-0 items-center justify-center gap-1 overflow-hidden rounded-4xl border border-transparent px-2 py-0.5 text-xs font-medium whitespace-nowrap transition-all focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 has-data-[icon=inline-end]:pr-1.5 has-data-[icon=inline-start]:pl-1.5 aria-invalid:border-destructive aria-invalid:ring-destructive/20 dark:aria-invalid:ring-destructive/40 [&>svg]:pointer-events-none [&>svg]:size-3!",
  {
    variants: {
      variant: {
        default:
          "bg-[var(--color-brand-primary)]/10 text-[var(--color-brand-primary)] dark:bg-teal-300/12 dark:text-teal-300 [a]:hover:bg-[var(--color-brand-primary)]/15",
        secondary:
          "bg-slate-100 text-slate-700 dark:bg-white/8 dark:text-slate-200 [a]:hover:bg-slate-200 dark:[a]:hover:bg-white/12",
        destructive:
          "bg-rose-50 text-rose-700 dark:bg-rose-500/15 dark:text-rose-300 focus-visible:ring-destructive/20 dark:focus-visible:ring-destructive/40 [a]:hover:bg-rose-100",
        outline:
          "border-slate-200 text-slate-700 dark:border-white/15 dark:text-slate-300 [a]:hover:bg-slate-50 dark:[a]:hover:bg-white/5",
        ghost:
          "text-slate-500 hover:bg-slate-100 hover:text-slate-700 dark:text-slate-400 dark:hover:bg-white/5 dark:hover:text-slate-200",
        link: "text-[var(--color-brand-primary)] underline-offset-4 hover:underline",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
)

function Badge({
  className,
  variant = "default",
  render,
  ...props
}: useRender.ComponentProps<"span"> & VariantProps<typeof badgeVariants>) {
  return useRender({
    defaultTagName: "span",
    props: mergeProps<"span">(
      {
        className: cn(badgeVariants({ variant }), className),
      },
      props
    ),
    render,
    state: {
      slot: "badge",
      variant,
    },
  })
}

export { Badge, badgeVariants }
