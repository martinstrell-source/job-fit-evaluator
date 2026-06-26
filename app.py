import pandas as pd
import streamlit as st
from concurrent.futures import ThreadPoolExecutor

from evaluator import (
    PIPELINE_STATUSES,
    save_resume,
    load_resume,
    save_evaluation,
    load_evaluations,
    update_status,
    update_evaluation,
    delete_evaluation,
    _extract_verdict,
    _verdict_score,
    _extract_job_title,
    fetch_job_description,
    extract_company_name,
    research_company,
    _run_gpt4o,
    _run_claude,
    _run_synthesis,
)

st.set_page_config(page_title="AI Job Evaluator", layout="wide")
st.title("AI Job Evaluator")


# --- Session state init ---
for key in ("job_description_area", "company_name", "company_research", "posting_age"):
    if key not in st.session_state:
        st.session_state[key] = ""

if "resume_area" not in st.session_state:
    st.session_state["resume_area"] = load_resume()


def _on_resume_change() -> None:
    save_resume(st.session_state["resume_area"])


# ============================================================
# TABS
# ============================================================
tab_eval, tab_pipeline = st.tabs(["Evaluator", "Pipeline"])

# ============================================================
# TAB 1 — EVALUATOR
# ============================================================
with tab_eval:
    col1, col2 = st.columns(2)

    with col1:
        resume = st.text_area(
            "Resume",
            height=400,
            placeholder="Paste resume here...",
            key="resume_area",
            on_change=_on_resume_change,
        )

    with col2:
        job_url = st.text_input("Job URL (optional)", placeholder="https://...")
        fetch_clicked = st.button("Fetch Job Description", use_container_width=True)

        if fetch_clicked:
            if not job_url.strip():
                st.warning("Enter a URL first.")
            elif "linkedin.com" in job_url.lower():
                st.error("LinkedIn blocks automated access. Try copying the URL from the company's careers page or the Apply button destination instead.")
            else:
                with st.spinner("Fetching job description..."):
                    text, error, posting_age = fetch_job_description(job_url.strip())
                if error:
                    st.error(error)
                else:
                    st.session_state["job_description_area"] = text
                    st.session_state["posting_age"] = posting_age
                    st.success("Job description fetched." + (f" · {posting_age}" if posting_age else ""))
                    if "TAVILY_API_KEY" in st.secrets:
                        with st.spinner("Researching company..."):
                            try:
                                name = extract_company_name(text, st.secrets["OPENAI_API_KEY"])
                                research = research_company(name, st.secrets["TAVILY_API_KEY"], st.secrets["OPENAI_API_KEY"])
                                st.session_state["company_name"] = name
                                st.session_state["company_research"] = research
                            except Exception as e:
                                st.session_state["company_research"] = ""
                                st.caption(f"Company research unavailable: {e}")

        job_description = st.text_area(
            "Job Description",
            height=300,
            placeholder="Paste job description here, or fetch from a URL above...",
            key="job_description_area",
        )

        research_clicked = st.button("Research Company", use_container_width=True)
        if research_clicked:
            if not job_description.strip():
                st.warning("Paste a job description first.")
            elif "TAVILY_API_KEY" not in st.secrets:
                st.error("TAVILY_API_KEY is not set in secrets.toml.")
            else:
                with st.spinner("Researching company..."):
                    try:
                        name = extract_company_name(job_description, st.secrets["OPENAI_API_KEY"])
                        research = research_company(name, st.secrets["TAVILY_API_KEY"], st.secrets["OPENAI_API_KEY"])
                        st.session_state["company_name"] = name
                        st.session_state["company_research"] = research
                        st.success(f"Research complete for {name}.")
                    except Exception as e:
                        st.error(f"Company research failed: {e}")

    if st.session_state["company_research"]:
        with st.expander(f"Company Research: {st.session_state['company_name']}", expanded=False):
            st.markdown(st.session_state["company_research"])

    if st.button("Evaluate Fit", type="primary", use_container_width=True):
        if not resume.strip() or not job_description.strip():
            st.warning("Please paste both a resume and a job description.")
        else:
            company_research = st.session_state.get("company_research", "")
            posting_age = st.session_state.get("posting_age", "")
            job_block = f"JOB DESCRIPTION{f' ({posting_age})' if posting_age else ''}:\n{job_description}"
            user_content = f"RESUME:\n{resume}\n\n{job_block}"
            if company_research:
                user_content = f"COMPANY RESEARCH:\n{company_research}\n\n" + user_content

            with st.spinner("Evaluating with GPT-4o and Claude in parallel..."):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    gpt_future = executor.submit(_run_gpt4o, st.secrets["OPENAI_API_KEY"], user_content)
                    claude_future = executor.submit(_run_claude, st.secrets["ANTHROPIC_API_KEY"], user_content)
                    gpt_result = gpt_future.result()
                    claude_result = claude_future.result()

            st.divider()
            eval_col1, eval_col2 = st.columns(2)
            with eval_col1:
                st.subheader("GPT-4o")
                st.markdown(gpt_result)
            with eval_col2:
                st.subheader("Claude Sonnet 4")
                st.markdown(claude_result)

            st.divider()
            with st.spinner("Synthesizing..."):
                synthesis = _run_synthesis(st.secrets["ANTHROPIC_API_KEY"], gpt_result, claude_result)
            st.subheader("Synthesis")
            st.markdown(synthesis)

            # --- Auto-save to pipeline ---
            with st.spinner("Saving to pipeline..."):
                try:
                    company = st.session_state.get("company_name", "").strip()
                    if not company:
                        company = extract_company_name(job_description, st.secrets["OPENAI_API_KEY"])
                    job_title = _extract_job_title(job_description, st.secrets["OPENAI_API_KEY"])
                    save_evaluation(
                        company=company,
                        job_title=job_title,
                        gpt_verdict=_extract_verdict(gpt_result),
                        claude_verdict=_extract_verdict(claude_result),
                        synthesis=synthesis,
                        job_description=job_description,
                        gpt_analysis=gpt_result,
                        claude_analysis=claude_result,
                        company_research=company_research,
                    )
                    st.success("Saved to pipeline.")
                except Exception as e:
                    st.caption(f"Pipeline save failed: {e}")

# ============================================================
# TAB 2 — PIPELINE
# ============================================================
with tab_pipeline:
    st.header("Pipeline")

    df = load_evaluations()

    if df.empty:
        st.info("No evaluations yet. Run an evaluation to populate the pipeline.")
    else:
        # --- Derived scores + min-total filter ---
        total_s = df["gpt_verdict"].map(_verdict_score) + df["claude_verdict"].map(_verdict_score)
        min_total = st.number_input(
            "Minimum total score (GPT + Claude, out of 20)",
            min_value=0, max_value=20, value=0, step=1,
            help="Hide matches below this combined total. 0 shows everything; rows without a score are hidden when this is above 0.",
        )
        if min_total > 0:
            df = df[total_s >= min_total]
        df = df.reset_index(drop=True)

        if df.empty:
            st.info("No matches at or above the selected minimum total.")
        else:
            gpt_s = df["gpt_verdict"].map(_verdict_score)
            claude_s = df["claude_verdict"].map(_verdict_score)
            total_s = gpt_s + claude_s
            gap_s = gpt_s - claude_s
            source_s = df["source_id"].map(lambda v: "Auto" if (isinstance(v, str) and v) else "Manual")

            display_df = pd.DataFrame({
                "id": df["id"],
                "Found": df["created_at"],
                "Company": df["company"],
                "Job Title": df["job_title"],
                "GPT-4o Verdict": df["gpt_verdict"],
                "Claude Verdict": df["claude_verdict"],
                "Gap": gap_s.map(lambda x: f"{x:+.1f}" if pd.notna(x) else ""),
                "Total": total_s.map(lambda x: f"{x:.1f}" if pd.notna(x) else ""),
                "Source": source_s,
                "Status": df["status"],
                "Link": df["job_url"],
            })

            edited = st.data_editor(
                display_df,
                column_config={
                    "id": None,  # hide the id column
                    "Status": st.column_config.SelectboxColumn(
                        "Status",
                        options=PIPELINE_STATUSES,
                        required=True,
                    ),
                    "Link": st.column_config.LinkColumn("Link", display_text="Open"),
                },
                disabled=["Found", "Company", "Job Title", "GPT-4o Verdict", "Claude Verdict", "Gap", "Total", "Source", "Link"],
                hide_index=True,
                use_container_width=True,
            )

            # Persist any status changes
            changed = edited[edited["Status"] != display_df["Status"]]
            for _, row in changed.iterrows():
                update_status(int(row["id"]), row["Status"])
            if not changed.empty:
                st.toast(f"Updated {len(changed)} status{'es' if len(changed) > 1 else ''}.")

            # Detail view + delete
            st.divider()
            st.subheader("Evaluation details")
            options = {f"{r['company']} — {r['job_title']} ({r['created_at']})": i for i, r in df.iterrows()}
            detail_col, reeval_col, delete_col = st.columns([5, 1.5, 1])
            with detail_col:
                selected_label = st.selectbox("Select an evaluation", list(options.keys()), label_visibility="collapsed")
            with reeval_col:
                reeval_clicked = st.button("🔄 Re-evaluate", use_container_width=True)
            with delete_col:
                delete_clicked = st.button("🗑 Delete", use_container_width=True)

            if selected_label:
                selected_row = df.iloc[options[selected_label]]

                if delete_clicked:
                    delete_evaluation(int(selected_row["id"]))
                    st.toast(f"Deleted {selected_row['company']} — {selected_row['job_title']}.")
                    st.rerun()

                if reeval_clicked:
                    jd = selected_row.get("job_description", "") or ""
                    resume_text = load_resume()
                    if not jd.strip():
                        st.error("No job description stored for this entry — open it in the Evaluator tab and run a fresh evaluation.")
                    elif not resume_text.strip():
                        st.error("No resume found. Paste your resume in the Evaluator tab first.")
                    else:
                        company_name = (selected_row.get("company") or "").strip()
                        research = ""
                        if company_name and "TAVILY_API_KEY" in st.secrets:
                            with st.spinner("Researching company..."):
                                try:
                                    research = research_company(
                                        company_name, st.secrets["TAVILY_API_KEY"], st.secrets["OPENAI_API_KEY"]
                                    )
                                except Exception:
                                    research = ""
                        user_content = f"RESUME:\n{resume_text}\n\nJOB DESCRIPTION:\n{jd}"
                        if research:
                            user_content = f"COMPANY RESEARCH:\n{research}\n\n" + user_content
                        with st.spinner("Re-evaluating with GPT-4o and Claude in parallel..."):
                            with ThreadPoolExecutor(max_workers=2) as executor:
                                gpt_future = executor.submit(_run_gpt4o, st.secrets["OPENAI_API_KEY"], user_content)
                                claude_future = executor.submit(_run_claude, st.secrets["ANTHROPIC_API_KEY"], user_content)
                                gpt_result = gpt_future.result()
                                claude_result = claude_future.result()
                        with st.spinner("Synthesizing..."):
                            synthesis = _run_synthesis(st.secrets["ANTHROPIC_API_KEY"], gpt_result, claude_result)
                        update_evaluation(
                            row_id=int(selected_row["id"]),
                            gpt_verdict=_extract_verdict(gpt_result),
                            claude_verdict=_extract_verdict(claude_result),
                            synthesis=synthesis,
                            gpt_analysis=gpt_result,
                            claude_analysis=claude_result,
                            company_research=(research or None),
                        )
                        st.toast(f"Re-evaluated {selected_row['company']} — {selected_row['job_title']}.")
                        st.rerun()

                if selected_row.get("job_url"):
                    st.markdown(f"[Open job posting ↗]({selected_row['job_url']})")

                research = selected_row.get("company_research")
                if isinstance(research, str) and research.strip():
                    with st.expander("Business overview", expanded=False):
                        st.markdown(research)

                gpt_a = selected_row.get("gpt_analysis")
                claude_a = selected_row.get("claude_analysis")
                if (isinstance(gpt_a, str) and gpt_a.strip()) or (isinstance(claude_a, str) and claude_a.strip()):
                    ac1, ac2 = st.columns(2)
                    with ac1:
                        if isinstance(gpt_a, str) and gpt_a.strip():
                            with st.expander("GPT-4o analysis", expanded=False):
                                st.markdown(gpt_a)
                    with ac2:
                        if isinstance(claude_a, str) and claude_a.strip():
                            with st.expander("Claude analysis", expanded=False):
                                st.markdown(claude_a)

                with st.expander("Synthesis (agreement & differences)", expanded=True):
                    st.markdown(selected_row["synthesis"])
