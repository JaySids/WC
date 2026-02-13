import asyncio
import json
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        
        # Set a large viewport to see the sidebar clearly
        await page.set_viewport_size({"width": 1600, "height": 1000})
        
        # Replace this with the specific Daytona workspace URL you are testing
        await page.goto("https://www.daytona.io/", wait_until="networkidle") 
        
        print("Injecting Set-of-Mark with 'Cursor Pointer' detection...")

        # --- THE UPDATED LOGIC ---
        interactive_map = await page.evaluate("""() => {
            const items = [];
            let counter = 1;

            // Helper: Check if element is visible
            function isVisible(elem) {
                if (!elem) return false;
                const style = window.getComputedStyle(elem);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                const rect = elem.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            }

            // 1. Find ALL elements in the DOM
            const allElements = document.querySelectorAll('*');

            // 2. Filter for "cursor: pointer" OR standard interactive tags
            const candidates = Array.from(allElements).filter(el => {
                if (!isVisible(el)) return false;
                
                const style = window.getComputedStyle(el);
                const tagName = el.tagName.toLowerCase();
                
                // CRITICAL: If the cursor is a pointer, it's clickable.
                // This catches the 'div' buttons in file explorers.
                const isPointer = style.cursor === 'pointer';
                
                // Standard interactive elements
                const isInteractiveTag = ['button', 'a', 'input', 'select', 'textarea'].includes(tagName);
                const isRoleButton = el.getAttribute('role') === 'button';

                return isPointer || isInteractiveTag || isRoleButton;
            });

            // 3. Create Overlay Layer
            const labelContainer = document.createElement('div');
            Object.assign(labelContainer.style, {
                position: 'fixed', top: '0', left: '0', width: '100vw', height: '100vh',
                pointerEvents: 'none', zIndex: '2147483647'
            });
            document.body.appendChild(labelContainer);

            // 4. Draw labels
            candidates.forEach((el) => {
                const rect = el.getBoundingClientRect();

                // LOWER THRESHOLD: 
                // We allow 10x10 elements now to catch small icons (like 'x' to close tabs)
                if (rect.width < 10 || rect.height < 10) return;

                // Deduplication: 
                // If a parent and child are both clickable (common in React),
                // only label the child to avoid clutter.
                // (Simple check: is there another candidate strictly inside this one?)
                const hasChildCandidate = candidates.some(c => 
                    c !== el && c.contains(el) === false && el.contains(c)
                );
                // Note: For complex IDEs, you might actually WANT both, 
                // but let's stick to the element itself for now.

                const label = document.createElement('div');
                label.innerText = counter;
                Object.assign(label.style, {
                    position: 'absolute',
                    left: (rect.left - 2) + 'px', // Slight offset to not cover the icon
                    top: (rect.top - 2) + 'px',
                    backgroundColor: 'rgba(255, 0, 0, 0.9)', // Semi-transparent red
                    color: 'white',
                    fontSize: '10px', // Smaller font for dense UI
                    fontWeight: 'bold',
                    padding: '1px 3px',
                    borderRadius: '2px',
                    boxShadow: '0 1px 2px rgba(0,0,0,0.5)',
                    zIndex: '2147483647'
                });
                labelContainer.appendChild(label);

                items.push({
                    id: counter,
                    tag: el.tagName.toLowerCase(),
                    text: el.innerText ? el.innerText.slice(0, 20) : '',
                    aria: el.getAttribute('aria-label') || '',
                    title: el.getAttribute('title') || '', // Very important for icon-only buttons!
                    x: rect.x,
                    y: rect.y
                });
                counter++;
            });

            return items;
        }""")

        print(f"Found {len(interactive_map)} interactive elements.")

        await page.screenshot(path="annotated_daytona.png", full_page=False)
        
        with open("daytona_map.json", "w") as f:
            json.dump(interactive_map, f, indent=2)

        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())