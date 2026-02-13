# Interactive Element Scraping — Claude Code Prompt

Problem: The scraper captures the page in ONE state. Interactive elements (tabs, accordions, toggles, "show more" buttons) have hidden content that only appears after clicking. The clone renders the buttons but they do nothing because Claude never saw the hidden content.

Fix: During scraping, DETECT interactive elements, CLICK each one, CAPTURE the content that appears, and PASS all states to Claude so it can wire up the interactivity.

**Add this to the existing scraper.py.**

---

## What to Detect and Click

| Pattern | How to Detect | What to Capture |
|---------|--------------|-----------------|
| Tabs | Multiple buttons/links in a row, one "active" (different style), adjacent content panel | Click each tab → capture panel content per tab |
| Accordion/FAQ | Question + chevron/plus icon, answer hidden | Click each question → capture answer text |
| Code tabs | Buttons like "Python", "JavaScript", "cURL" near a code block | Click each → capture code content per language |
| Toggle switch | Two labels + switch element, content changes | Click toggle → capture both states |
| "Show more" / "Read more" | Button that expands truncated content | Click → capture full content |
| Dropdown nav | Nav item with chevron, submenu hidden | Hover/click → capture submenu links |
| Modal trigger | Button that opens a dialog/popup | Click → capture modal content |

---

## Implementation

Add to `backend/app/scraper.py`:

```python
async def scrape_interactive_elements(page) -> list:
    """
    Detect interactive UI patterns, click through each state,
    and capture the content behind each interaction.
    
    Returns a list of interactive element groups:
    [
        {
            "type": "tabs",
            "container_selector": "section.api-section",
            "position": {"x": 500, "y": 2400},
            "tabs": [
                {
                    "label": "Process Execution",
                    "active": true,
                    "content_text": "from daytona import...",
                    "content_html_snippet": "<pre><code>...</code></pre>"
                },
                {
                    "label": "File System Operations",
                    "active": false,
                    "content_text": "sandbox.fs.upload_file...",
                    "content_html_snippet": "<pre><code>...</code></pre>"
                },
                ...
            ]
        },
        {
            "type": "accordion",
            "items": [
                {"question": "What is Daytona?", "answer": "Daytona is..."},
                {"question": "How does pricing work?", "answer": "We offer..."},
            ]
        },
        {
            "type": "toggle",
            "labels": ["Monthly", "Annual"],
            "states": {
                "Monthly": {"content_text": "$49/mo..."},
                "Annual": {"content_text": "$39/mo..."}
            }
        }
    ]
    """
    
    interactives = []
    
    # --- DETECT TABS ---
    tab_groups = await detect_and_scrape_tabs(page)
    interactives.extend(tab_groups)
    
    # --- DETECT ACCORDIONS ---
    accordion_groups = await detect_and_scrape_accordions(page)
    interactives.extend(accordion_groups)
    
    # --- DETECT TOGGLES (pricing monthly/yearly) ---
    toggle_groups = await detect_and_scrape_toggles(page)
    interactives.extend(toggle_groups)
    
    # --- DETECT DROPDOWNS (nav submenus) ---
    dropdown_groups = await detect_and_scrape_dropdowns(page)
    interactives.extend(dropdown_groups)
    
    return interactives


async def detect_and_scrape_tabs(page) -> list:
    """
    Find tab-like UI patterns and click through each tab to capture content.
    
    Detection strategy:
    1. Find groups of buttons/links where one has an "active" style
    2. Find adjacent content panel that changes when tabs are clicked
    """
    
    tab_groups = await page.evaluate('''() => {
        const groups = [];
        
        // Strategy 1: Find role="tablist" (accessible tabs)
        const tablists = document.querySelectorAll('[role="tablist"]');
        for (const tablist of tablists) {
            const tabs = tablist.querySelectorAll('[role="tab"]');
            if (tabs.length >= 2) {
                const rect = tablist.getBoundingClientRect();
                groups.push({
                    strategy: "aria",
                    selector: getUniqueSelector(tablist),
                    position: { x: rect.x, y: rect.y },
                    tabs: Array.from(tabs).map(tab => ({
                        label: tab.textContent.trim(),
                        selector: getUniqueSelector(tab),
                        isActive: tab.getAttribute("aria-selected") === "true" || 
                                  tab.classList.contains("active") ||
                                  tab.getAttribute("data-state") === "active",
                    }))
                });
            }
        }
        
        // Strategy 2: Find button groups that look like tabs
        // (horizontal row of buttons/links where one is visually different)
        const buttonGroups = document.querySelectorAll(
            '.tabs, .tab-list, .tab-nav, .tab-buttons, .tab-header, ' +
            '[class*="tab-list"], [class*="tabList"], [class*="TabList"], ' +
            '[class*="tab-nav"], [class*="tabNav"], [class*="TabNav"]'
        );
        
        for (const group of buttonGroups) {
            const buttons = group.querySelectorAll('button, a, [role="tab"]');
            if (buttons.length >= 2 && buttons.length <= 10) {
                const rect = group.getBoundingClientRect();
                // Check if they're roughly horizontal (tab-like layout)
                const rects = Array.from(buttons).map(b => b.getBoundingClientRect());
                const isHorizontal = rects.every(r => Math.abs(r.y - rects[0].y) < 30);
                
                if (isHorizontal || buttons.length <= 5) {
                    groups.push({
                        strategy: "class",
                        selector: getUniqueSelector(group),
                        position: { x: rect.x, y: rect.y },
                        tabs: Array.from(buttons).map(btn => ({
                            label: btn.textContent.trim(),
                            selector: getUniqueSelector(btn),
                            isActive: hasActiveStyle(btn),
                        }))
                    });
                }
            }
        }
        
        // Strategy 3: Find adjacent button groups near code blocks
        // (common pattern: language selector above <pre><code>)
        const codeBlocks = document.querySelectorAll('pre');
        for (const code of codeBlocks) {
            const parent = code.parentElement;
            if (!parent) continue;
            
            // Look for button row in the parent or previous sibling
            const buttonContainer = parent.querySelector('.flex, .inline-flex, [class*="button"]') ||
                                    code.previousElementSibling;
            
            if (buttonContainer) {
                const btns = buttonContainer.querySelectorAll('button, a');
                if (btns.length >= 2 && btns.length <= 8) {
                    const rect = buttonContainer.getBoundingClientRect();
                    groups.push({
                        strategy: "code-tabs",
                        selector: getUniqueSelector(buttonContainer),
                        position: { x: rect.x, y: rect.y },
                        tabs: Array.from(btns).map(btn => ({
                            label: btn.textContent.trim(),
                            selector: getUniqueSelector(btn),
                            isActive: hasActiveStyle(btn),
                        }))
                    });
                }
            }
        }
        
        // Helper: generate a unique CSS selector for an element
        function getUniqueSelector(el) {
            if (el.id) return '#' + el.id;
            
            const path = [];
            while (el && el !== document.body) {
                let selector = el.tagName.toLowerCase();
                if (el.id) {
                    selector = '#' + el.id;
                    path.unshift(selector);
                    break;
                }
                if (el.className && typeof el.className === 'string') {
                    const classes = el.className.split(' ')
                        .filter(c => c && !c.startsWith('framer-') && c.length < 30)
                        .slice(0, 2);
                    if (classes.length) selector += '.' + classes.join('.');
                }
                // Add nth-child if needed
                const parent = el.parentElement;
                if (parent) {
                    const siblings = Array.from(parent.children).filter(c => c.tagName === el.tagName);
                    if (siblings.length > 1) {
                        const index = siblings.indexOf(el) + 1;
                        selector += ':nth-child(' + index + ')';
                    }
                }
                path.unshift(selector);
                el = el.parentElement;
            }
            return path.join(' > ');
        }
        
        // Helper: check if element has "active" visual treatment
        function hasActiveStyle(el) {
            const style = getComputedStyle(el);
            const classes = el.className || '';
            
            return el.getAttribute("aria-selected") === "true" ||
                   el.getAttribute("data-state") === "active" ||
                   classes.includes("active") ||
                   classes.includes("selected") ||
                   classes.includes("current") ||
                   // Check if it has a different background than siblings
                   (style.backgroundColor !== 'rgba(0, 0, 0, 0)' && 
                    style.backgroundColor !== 'transparent');
        }
        
        // Deduplicate groups (same tabs might be found by multiple strategies)
        const seen = new Set();
        return groups.filter(g => {
            const key = g.tabs.map(t => t.label).join('|');
            if (seen.has(key)) return false;
            seen.add(key);
            return true;
        });
    }''')
    
    if not tab_groups:
        return []
    
    results = []
    
    for group in tab_groups:
        tab_data = {
            "type": "tabs",
            "position": group["position"],
            "tabs": []
        }
        
        for tab_info in group["tabs"]:
            label = tab_info["label"]
            selector = tab_info["selector"]
            
            # Click the tab
            try:
                await page.click(selector, timeout=3000)
                await page.wait_for_timeout(500)  # Wait for content transition
                
                # Capture the content panel that's now visible
                # Look for adjacent panel, role="tabpanel", or content that changed
                panel_content = await page.evaluate('''(tabSelector) => {
                    const tab = document.querySelector(tabSelector);
                    if (!tab) return null;
                    
                    // Strategy 1: aria-controls points to panel
                    const panelId = tab.getAttribute("aria-controls");
                    if (panelId) {
                        const panel = document.getElementById(panelId);
                        if (panel) return {
                            text: panel.innerText.trim(),
                            html: panel.innerHTML.substring(0, 2000),
                            hasCode: !!panel.querySelector('pre, code'),
                        };
                    }
                    
                    // Strategy 2: Find visible tabpanel sibling
                    const panels = document.querySelectorAll('[role="tabpanel"]');
                    for (const panel of panels) {
                        const style = getComputedStyle(panel);
                        if (style.display !== 'none' && style.visibility !== 'hidden' && panel.offsetHeight > 0) {
                            return {
                                text: panel.innerText.trim(),
                                html: panel.innerHTML.substring(0, 2000),
                                hasCode: !!panel.querySelector('pre, code'),
                            };
                        }
                    }
                    
                    // Strategy 3: Find the nearest visible content block after the tab bar
                    const tabParent = tab.closest('[role="tablist"]') || tab.parentElement;
                    if (tabParent && tabParent.nextElementSibling) {
                        const next = tabParent.nextElementSibling;
                        return {
                            text: next.innerText.trim(),
                            html: next.innerHTML.substring(0, 2000),
                            hasCode: !!next.querySelector('pre, code'),
                        };
                    }
                    
                    return null;
                }''', selector)
                
                tab_data["tabs"].append({
                    "label": label,
                    "is_active": tab_info["isActive"],
                    "content_text": panel_content["text"][:1500] if panel_content else "",
                    "has_code": panel_content.get("hasCode", False) if panel_content else False,
                })
                
            except Exception as e:
                # Tab click failed — still record the label
                tab_data["tabs"].append({
                    "label": label,
                    "is_active": tab_info["isActive"],
                    "content_text": "",
                    "click_failed": True,
                })
        
        # Only include if we got content for at least 2 tabs
        tabs_with_content = [t for t in tab_data["tabs"] if t.get("content_text")]
        if len(tabs_with_content) >= 2:
            results.append(tab_data)
    
    return results


async def detect_and_scrape_accordions(page) -> list:
    """
    Find accordion/FAQ patterns and click to expand each item.
    """
    
    accordion_items = await page.evaluate('''() => {
        const items = [];
        
        // Strategy 1: Find details/summary (native HTML accordion)
        const details = document.querySelectorAll('details');
        for (const d of details) {
            const summary = d.querySelector('summary');
            if (summary) {
                items.push({
                    strategy: "details",
                    question: summary.textContent.trim(),
                    selector: getUniqueSelector(summary),
                    isOpen: d.hasAttribute('open'),
                    answer: d.open ? d.textContent.replace(summary.textContent, '').trim() : '',
                });
            }
        }
        
        // Strategy 2: Find Disclosure/Accordion patterns
        // Button + collapsible panel
        const disclosureButtons = document.querySelectorAll(
            '[aria-expanded], ' +
            'button[class*="accordion"], button[class*="Accordion"], ' +
            'button[class*="disclosure"], button[class*="Disclosure"], ' +
            'button[class*="faq"], button[class*="FAQ"], ' +
            '[class*="accordion-trigger"], [class*="AccordionTrigger"]'
        );
        
        for (const btn of disclosureButtons) {
            const text = btn.textContent.trim();
            if (text.length > 5 && text.length < 200) {
                items.push({
                    strategy: "aria",
                    question: text,
                    selector: getUniqueSelector(btn),
                    isOpen: btn.getAttribute('aria-expanded') === 'true',
                });
            }
        }
        
        // Strategy 3: Heuristic — find question-like elements followed by hidden content
        // Look for elements with ? in text or FAQ-section headings
        const faqSections = document.querySelectorAll(
            '[class*="faq"], [class*="FAQ"], [class*="accordion"], [class*="Accordion"]'
        );
        
        for (const section of faqSections) {
            const clickables = section.querySelectorAll('button, [role="button"], summary, dt');
            for (const btn of clickables) {
                const text = btn.textContent.trim();
                if (text.length > 10 && text.length < 300 && !items.some(i => i.question === text)) {
                    items.push({
                        strategy: "heuristic",
                        question: text,
                        selector: getUniqueSelector(btn),
                        isOpen: false,
                    });
                }
            }
        }
        
        function getUniqueSelector(el) {
            if (el.id) return '#' + el.id;
            const path = [];
            while (el && el !== document.body) {
                let s = el.tagName.toLowerCase();
                if (el.id) { path.unshift('#' + el.id); break; }
                const parent = el.parentElement;
                if (parent) {
                    const siblings = Array.from(parent.children).filter(c => c.tagName === el.tagName);
                    if (siblings.length > 1) s += ':nth-child(' + (siblings.indexOf(el) + 1) + ')';
                }
                path.unshift(s);
                el = el.parentElement;
            }
            return path.join(' > ');
        }
        
        // Deduplicate
        const seen = new Set();
        return items.filter(item => {
            if (seen.has(item.question)) return false;
            seen.add(item.question);
            return true;
        });
    }''')
    
    if not accordion_items:
        return []
    
    result_items = []
    
    for item in accordion_items:
        question = item["question"]
        selector = item["selector"]
        
        # If already open and has answer, use it
        if item.get("answer"):
            result_items.append({
                "question": question,
                "answer": item["answer"][:1000],
            })
            continue
        
        # Click to open
        try:
            await page.click(selector, timeout=3000)
            await page.wait_for_timeout(400)
            
            # Capture the revealed content
            answer = await page.evaluate('''(selector) => {
                const btn = document.querySelector(selector);
                if (!btn) return "";
                
                // Check aria-controls
                const controlsId = btn.getAttribute("aria-controls");
                if (controlsId) {
                    const panel = document.getElementById(controlsId);
                    if (panel) return panel.innerText.trim();
                }
                
                // Check next sibling
                const parent = btn.closest('[class*="accordion"], [class*="disclosure"], details, [class*="faq"]') || btn.parentElement;
                if (parent) {
                    // Get text that's NOT the button text
                    const fullText = parent.innerText.trim();
                    const btnText = btn.innerText.trim();
                    const answer = fullText.replace(btnText, '').trim();
                    if (answer.length > 10) return answer;
                }
                
                // Check next element sibling
                const next = btn.nextElementSibling;
                if (next && next.offsetHeight > 0) {
                    return next.innerText.trim();
                }
                
                return "";
            }''', selector)
            
            if answer:
                result_items.append({
                    "question": question,
                    "answer": answer[:1000],
                })
            
            # Click again to close (restore original state)
            try:
                await page.click(selector, timeout=2000)
                await page.wait_for_timeout(200)
            except:
                pass
                
        except Exception:
            result_items.append({
                "question": question,
                "answer": "",
                "click_failed": True,
            })
    
    if len(result_items) < 2:
        return []
    
    return [{
        "type": "accordion",
        "items": result_items,
    }]


async def detect_and_scrape_toggles(page) -> list:
    """
    Find toggle switches (like monthly/yearly pricing) and capture both states.
    """
    
    toggles = await page.evaluate('''() => {
        const results = [];
        
        // Find toggle/switch elements
        const switches = document.querySelectorAll(
            '[role="switch"], ' +
            'input[type="checkbox"][class*="toggle"], ' +
            'button[class*="toggle"], button[class*="Toggle"], ' +
            'button[class*="switch"], button[class*="Switch"], ' +
            '[class*="pricing-toggle"], [class*="PricingToggle"]'
        );
        
        for (const sw of switches) {
            // Find the labels on either side
            const parent = sw.closest('.flex, .inline-flex, [class*="toggle"], [class*="pricing"]') || sw.parentElement;
            if (!parent) continue;
            
            const textNodes = Array.from(parent.querySelectorAll('span, label, p'))
                .map(el => el.textContent.trim())
                .filter(t => t.length > 0 && t.length < 30);
            
            if (textNodes.length >= 2) {
                results.push({
                    selector: getUniqueSelector(sw),
                    labels: textNodes.slice(0, 2),
                    isChecked: sw.getAttribute("aria-checked") === "true" || sw.checked,
                });
            }
        }
        
        function getUniqueSelector(el) {
            if (el.id) return '#' + el.id;
            const path = [];
            while (el && el !== document.body) {
                let s = el.tagName.toLowerCase();
                if (el.id) { path.unshift('#' + el.id); break; }
                const parent = el.parentElement;
                if (parent) {
                    const siblings = Array.from(parent.children).filter(c => c.tagName === el.tagName);
                    if (siblings.length > 1) s += ':nth-child(' + (siblings.indexOf(el) + 1) + ')';
                }
                path.unshift(s);
                el = el.parentElement;
            }
            return path.join(' > ');
        }
        
        return results;
    }''')
    
    if not toggles:
        return []
    
    results = []
    
    for toggle in toggles:
        selector = toggle["selector"]
        labels = toggle["labels"]
        
        # Capture current state content
        state_a_content = await page.evaluate('''() => {
            const pricing = document.querySelector('[class*="pricing"], [class*="Pricing"], [class*="plans"], [class*="Plans"]');
            return pricing ? pricing.innerText.trim().substring(0, 1500) : "";
        }''')
        
        # Click toggle
        try:
            await page.click(selector, timeout=3000)
            await page.wait_for_timeout(500)
            
            # Capture new state content
            state_b_content = await page.evaluate('''() => {
                const pricing = document.querySelector('[class*="pricing"], [class*="Pricing"], [class*="plans"], [class*="Plans"]');
                return pricing ? pricing.innerText.trim().substring(0, 1500) : "";
            }''')
            
            # Only include if content actually changed
            if state_a_content != state_b_content:
                results.append({
                    "type": "toggle",
                    "labels": labels,
                    "states": {
                        labels[0]: {"content_text": state_a_content},
                        labels[1]: {"content_text": state_b_content},
                    }
                })
            
            # Click back to restore
            try:
                await page.click(selector, timeout=2000)
                await page.wait_for_timeout(200)
            except:
                pass
                
        except Exception:
            pass
    
    return results


async def detect_and_scrape_dropdowns(page) -> list:
    """
    Find nav dropdown menus and hover/click to reveal submenus.
    """
    
    nav_dropdowns = await page.evaluate('''() => {
        const results = [];
        
        const navItems = document.querySelectorAll(
            'nav a, nav button, header a, header button, ' +
            '[class*="nav"] a, [class*="nav"] button'
        );
        
        for (const item of navItems) {
            // Check if it has a dropdown indicator
            const hasChevron = item.querySelector('svg') !== null;
            const hasAriaExpanded = item.hasAttribute('aria-expanded');
            const hasSubmenu = item.getAttribute('aria-haspopup') === 'true';
            
            if (hasChevron || hasAriaExpanded || hasSubmenu) {
                results.push({
                    label: item.textContent.trim(),
                    selector: getUniqueSelector(item),
                    href: item.getAttribute('href') || '',
                });
            }
        }
        
        function getUniqueSelector(el) {
            if (el.id) return '#' + el.id;
            const path = [];
            while (el && el !== document.body) {
                let s = el.tagName.toLowerCase();
                if (el.id) { path.unshift('#' + el.id); break; }
                const parent = el.parentElement;
                if (parent) {
                    const siblings = Array.from(parent.children).filter(c => c.tagName === el.tagName);
                    if (siblings.length > 1) s += ':nth-child(' + (siblings.indexOf(el) + 1) + ')';
                }
                path.unshift(s);
                el = el.parentElement;
            }
            return path.join(' > ');
        }
        
        return results;
    }''')
    
    if not nav_dropdowns:
        return []
    
    results = []
    
    for dropdown in nav_dropdowns:
        selector = dropdown["selector"]
        label = dropdown["label"]
        
        try:
            # Hover first (desktop dropdowns often open on hover)
            await page.hover(selector, timeout=2000)
            await page.wait_for_timeout(400)
            
            # Check if a submenu appeared
            sub_links = await page.evaluate('''(selector) => {
                const trigger = document.querySelector(selector);
                if (!trigger) return [];
                
                // Look for newly visible menus
                const menus = document.querySelectorAll(
                    '[role="menu"], [class*="dropdown"], [class*="submenu"], ' +
                    '[class*="Dropdown"], [class*="SubMenu"], [class*="popover"], [class*="Popover"]'
                );
                
                for (const menu of menus) {
                    const style = getComputedStyle(menu);
                    if (style.display !== 'none' && style.visibility !== 'hidden' && 
                        style.opacity !== '0' && menu.offsetHeight > 0) {
                        const links = Array.from(menu.querySelectorAll('a, button'));
                        return links.map(l => ({
                            text: l.textContent.trim(),
                            href: l.getAttribute('href') || '',
                        })).filter(l => l.text.length > 0 && l.text.length < 100);
                    }
                }
                
                return [];
            }''', selector)
            
            if sub_links and len(sub_links) >= 2:
                results.append({
                    "type": "dropdown",
                    "trigger_label": label,
                    "sub_links": sub_links[:15],
                })
            
            # Move mouse away to close dropdown
            await page.mouse.move(0, 0)
            await page.wait_for_timeout(300)
            
        except Exception:
            pass
    
    return results
```

---

## Wire Into Main Scrape Function

In `scraper.py`, add to `scrape_website()`:

```python
async def scrape_website(url: str) -> dict:
    # ... existing extraction code ...
    
    # After all other extraction, before closing browser:
    
    # Scrape interactive elements (tabs, accordions, toggles, dropdowns)
    interactive_elements = await scrape_interactive_elements(page)
    data["interactives"] = interactive_elements
    
    # ... close browser, return data ...
```

---

## Include in Preprocessor Summary

In `scrape_preprocessor.py`, add interactives to the summary:

```python
def preprocess_scrape(data: dict) -> dict:
    # ... existing summary building ...
    
    # Interactive elements: include in full (they're already compact)
    # This is what tells Claude to make tabs/accordions actually work
    interactives = data.get("interactives", [])
    
    if interactives:
        # Budget: ~2000 tokens for interactives
        interactive_summary = []
        tokens_used = 0
        
        for item in interactives:
            item_str = json.dumps(item)
            item_tokens = estimate_tokens(item_str)
            
            if tokens_used + item_tokens > 2000:
                # Truncate content within items to fit
                if item["type"] == "tabs":
                    for tab in item.get("tabs", []):
                        tab["content_text"] = tab.get("content_text", "")[:300]
                elif item["type"] == "accordion":
                    for acc in item.get("items", []):
                        acc["answer"] = acc.get("answer", "")[:200]
                elif item["type"] == "toggle":
                    for state_key, state_val in item.get("states", {}).items():
                        state_val["content_text"] = state_val.get("content_text", "")[:300]
            
            interactive_summary.append(item)
            tokens_used += estimate_tokens(json.dumps(item))
            
            if tokens_used > 2000:
                break
        
        summary["interactives"] = interactive_summary
    
    return summary
```

---

## Update System Prompt

Add this to the REACT_SYSTEM_PROMPT in `agent.py`:

```
=================================================================
SECTION 3.5: INTERACTIVE ELEMENTS FROM SCRAPE DATA
=================================================================

The scrape data may include an "interactives" array containing elements that were
ACTUALLY CLICKED during scraping. Each entry has the real content behind each interaction.

### TABS
If you see:
{
  "type": "tabs",
  "tabs": [
    {"label": "Process Execution", "content_text": "from daytona import..."},
    {"label": "File System", "content_text": "sandbox.fs.upload..."},
    {"label": "Git Integration", "content_text": "sandbox.git.clone..."}
  ]
}

Then implement WORKING TABS:
- Use @headlessui/react Tab or useState to switch between panels
- Each tab's content_text becomes the panel content
- The first tab with is_active:true should be the default selected tab
- If content is code (has_code:true), render it in a styled <pre><code> block
- Include ALL tabs with ALL their content — do not skip any

### ACCORDION / FAQ
If you see:
{
  "type": "accordion",
  "items": [
    {"question": "What is Daytona?", "answer": "Daytona is a secure..."},
    {"question": "How does pricing work?", "answer": "We offer three tiers..."}
  ]
}

Then implement WORKING ACCORDIONS:
- Use @headlessui/react Disclosure for each item
- question = the clickable trigger, answer = the expandable content
- Include ALL items — every question/answer pair
- Add chevron rotation animation on expand/collapse
- First item can be open by default

### TOGGLE (pricing monthly/yearly)
If you see:
{
  "type": "toggle",
  "labels": ["Monthly", "Annual"],
  "states": {
    "Monthly": {"content_text": "$49/mo per seat..."},
    "Annual": {"content_text": "$39/mo per seat..."}
  }
}

Then implement a WORKING TOGGLE:
- useState to track which state is active
- Animated switch/toggle button between the two labels
- Show the corresponding content for each state
- Parse the content_text to extract prices and features

### DROPDOWN NAV
If you see:
{
  "type": "dropdown",
  "trigger_label": "Products",
  "sub_links": [
    {"text": "Sandbox SDK", "href": "/sdk"},
    {"text": "Enterprise", "href": "/enterprise"}
  ]
}

Then implement a WORKING DROPDOWN:
- Use @headlessui/react Menu or Popover
- Desktop: open on hover, mobile: open on click
- Include ALL sub_links with real hrefs

CRITICAL: If the "interactives" data exists, these elements MUST be functional in your clone.
Static buttons that do nothing are a FAILURE. The scraper clicked these elements and captured
what happens — you have all the data you need to make them work.
```

---

## Time Budget

The interaction scraping adds time to the scrape phase:

| Step | Time Added |
|------|-----------|
| Tab detection + scanning | ~1s |
| Clicking each tab (×4 typical) | ~3s |
| Accordion detection + clicking (×6 typical) | ~4s |
| Toggle detection + click | ~1.5s |
| Dropdown hover detection | ~2s |
| **Total** | **~10-12s** |

Worth it. The alternative is a clone where half the buttons do nothing.

To keep it bounded:
```python
# At the top of scrape_interactive_elements:
MAX_INTERACTIVE_TIME = 15  # seconds
start = time.time()

# Before each detection function:
if time.time() - start > MAX_INTERACTIVE_TIME:
    return interactives  # Return what we have so far
```

---

## What This Looks Like for daytona.io

The scraper would capture:

```json
{
  "interactives": [
    {
      "type": "tabs",
      "tabs": [
        {
          "label": "Process Execution",
          "content_text": "from daytona import Daytona, CreateSandboxParams\n\ndaytona = Daytona()\nparams = CreateSandboxParams(language=\"python\")\nsandbox = daytona.create(params)\n\nresponse = sandbox.process.code_run('print(\"Hello World!\")')\nprint(response.result)...",
          "has_code": true
        },
        {
          "label": "File System Operations",
          "content_text": "file_content = b\"Hello, World!\"\nsandbox.fs.upload_file(file_content, \"/home/daytona/data.txt\")\n\ndownloaded = sandbox.fs.download_file(\"/home/daytona/data.txt\")\nprint(downloaded.decode())...",
          "has_code": true
        },
        {
          "label": "Git Integration",
          "content_text": "sandbox.git.clone(\"https://github.com/user/repo.git\")\n...",
          "has_code": true
        },
        {
          "label": "Builtin LSP Support",
          "content_text": "completions = sandbox.lsp.completions(\"main.py\", {\"line\": 5, \"character\": 10})\n...",
          "has_code": true
        }
      ]
    }
  ]
}
```

Claude sees this and generates:

```jsx
const [activeTab, setActiveTab] = useState(0);

const tabs = [
  { label: "Process Execution", code: `from daytona import...` },
  { label: "File System Operations", code: `file_content = b"Hello"...` },
  { label: "Git Integration", code: `sandbox.git.clone(...)` },
  { label: "Builtin LSP Support", code: `completions = sandbox.lsp...` },
];

// ... renders actual working tabs with actual code content
```

Buttons work. Content switches. Because the scraper actually clicked them.
