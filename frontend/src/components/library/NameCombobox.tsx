import { useId } from "react";
import { Combobox } from "@base-ui/react/combobox";
import { Check, ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * A folder-name field: pick an existing name, or type a new one.
 *
 * The value *is* the text in the input, which is what makes this creatable
 * without a second "create" step — Base UI's own creatable example opens a
 * dialog to add an item to a list, but there is no list to add to here: a name
 * that matches no folder simply becomes a new folder when the move runs, and
 * the backend sanitises it. Selecting a suggestion and typing therefore write
 * the same state, and the popup is a filter over the library rather than a set
 * of allowed answers.
 */
export function NameCombobox({
  label,
  hint,
  placeholder,
  options,
  value,
  onChange,
}: {
  label: string;
  hint?: string;
  placeholder: string;
  options: readonly string[];
  value: string;
  onChange: (value: string) => void;
}) {
  const id = useId();

  return (
    <Combobox.Root
      items={options as string[]}
      value={value}
      onValueChange={(next) => onChange(typeof next === "string" ? next : "")}
      inputValue={value}
      onInputValueChange={onChange}
    >
      <div className="flex flex-col gap-1">
        <label htmlFor={id} className="text-sm font-medium">
          {label}
        </label>
        {hint !== undefined && (
          <span className="text-xs text-muted-foreground">{hint}</span>
        )}
        <Combobox.InputGroup className="relative flex h-8 items-center rounded-lg border border-input bg-background focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/50">
          <Combobox.Input
            id={id}
            autoComplete="off"
            placeholder={placeholder}
            className="h-full w-full rounded-lg bg-transparent px-2.5 text-sm outline-none placeholder:text-muted-foreground"
          />
          <Combobox.Trigger
            aria-label={`Show existing ${label.toLowerCase()} names`}
            className="flex h-full w-7 items-center justify-center text-muted-foreground"
          >
            <ChevronDown className="size-4" aria-hidden />
          </Combobox.Trigger>
        </Combobox.InputGroup>
      </div>

      <Combobox.Portal>
        <Combobox.Positioner className="isolate z-50" sideOffset={4}>
          <Combobox.Popup className="max-h-60 w-(--anchor-width) overflow-y-auto rounded-lg bg-popover p-1 text-sm text-popover-foreground shadow-md ring-1 ring-foreground/10 outline-none">
            {/* Stays mounted whatever the list holds, so screen readers hear
                the change; only its children come and go. */}
            <Combobox.Empty>
              <span className="block px-2 py-1.5 text-xs text-muted-foreground">
                No match — this name will be created.
              </span>
            </Combobox.Empty>
            <Combobox.List>
              {(item: string) => (
                <Combobox.Item
                  key={item}
                  value={item}
                  className={cn(
                    "flex cursor-default items-center gap-2 rounded-md px-2 py-1.5 outline-none select-none",
                    "data-highlighted:bg-muted data-highlighted:text-foreground",
                  )}
                >
                  <Combobox.ItemIndicator>
                    <Check className="size-3.5" aria-hidden />
                  </Combobox.ItemIndicator>
                  <span className="truncate">{item}</span>
                </Combobox.Item>
              )}
            </Combobox.List>
          </Combobox.Popup>
        </Combobox.Positioner>
      </Combobox.Portal>
    </Combobox.Root>
  );
}
