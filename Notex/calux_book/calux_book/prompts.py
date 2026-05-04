"""Prompt templates for Calux Book transformations and chat."""

from __future__ import annotations


def get_transformation_prompt(transform_type: str) -> str:
    dispatch = {
        "summary": _summary_prompt,
        "custom": _custom_prompt,
    }
    return dispatch.get(transform_type, _default_prompt)()


def _summary_prompt() -> str:
    return (
        "You are an expert at creating comprehensive summaries. "
        "Based on the following sources, create a {length} summary in {format} format.\n"
        "**Important: Always respond in English. Do not wrap the output in ```markdown``` tags.**\n\n"
        "Sources:\n{sources}\n\n"
        "Provide a well-structured summary that captures the key information, "
        "main topics, and important details from the sources."
    )


def _custom_prompt() -> str:
    return (
        "You are a helpful assistant. Based on the following sources and custom request, "
        "generate the requested content.\n"
        "**Important: Always respond in English. Do not wrap the output in ```markdown``` tags.**\n\n"
        "Sources:\n{sources}\n\n"
        "Custom request:\n{prompt}\n\n"
        "Please generate the content in {format} format, keeping it {length}."
    )


def _default_prompt() -> str:
    return (
        "You are a helpful assistant. Based on the following sources, "
        "provide a {type} in {format} format.\n"
        "**Important: Always respond in English. Do not wrap the output in ```markdown``` tags.**\n\n"
        "Sources:\n{sources}\n\n"
        "Generate {length} content."
    )


def chat_system_prompt() -> str:
    return (
        "You are a helpful AI assistant for a notebook application. "
        "Answer the user's questions based on the provided context and chat history.\n"
        "**Important: Always respond in English. Do not wrap the output in ```markdown``` tags.**\n\n"
        "The context below is organized by source, page number, and section. "
        "When referencing information, cite the source name and page number "
        "(e.g. 'According to URDPFI Guidelines, Page 42, ...'). "
        "If multiple sources are relevant, cite each one.\n"
        "If there is not enough information in the context, state this clearly "
        "and provide a general answer if possible.\n\n"
        "Chat history:\n{history}\n\n"
        "Context:\n{context}\n\n"
        "User question: {question}\n\n"
        "Provide a helpful, accurate, and well-structured answer with source citations."
    )
