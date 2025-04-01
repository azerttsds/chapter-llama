import argparse
from pathlib import Path

from lutils import writef

from src.data.single_video import SingleVideo
from src.data.utils_asr import PromptASR
from src.models.llama_inference import LlamaInference
from src.test.vidchapters import get_chapters
from tools.download.models import download_model


def main(video_path: Path, model: str = "asr-10k"):
    single_video = SingleVideo(video_path)
    prompt = PromptASR(chapters=single_video)

    vid_id = single_video.video_ids[0]
    prompt = prompt.get_prompt_test(vid_id)
    transcript = single_video.get_asr(vid_id)
    prompt = prompt + transcript

    model_path = download_model(model)
    inference = LlamaInference(
        ckpt_path="meta-llama/Llama-3.1-8B-Instruct", peft_model=model_path
    )

    output_text, chapters = get_chapters(
        inference,
        prompt,
        max_new_tokens=1024,
        do_sample=False,
        vid_id=vid_id,
    )

    print("\n===== 📝 Chapters =====")
    for timestamp, text in chapters.items():
        print(f"{timestamp}: {text}")
    print("=======================\n")

    output_dir = Path(f"outputs/inference/{video_path.stem}")
    output_dir.mkdir(parents=True, exist_ok=True)
    writef(output_dir / "output_text.txt", output_text)
    writef(output_dir / "chapters.json", chapters)
    print(f"Output saved to {output_dir}")


def cli_entrypoint():
    """Command-line entry point that parses arguments and calls the main function."""
    parser = argparse.ArgumentParser(
        description="Generate chapters for a single video."
    )
    parser.add_argument("video_path", type=Path, help="Path to the video file.")
    parser.add_argument(
        "--model",
        type=str,
        default="asr-10k",
        help="Chapter-Llama model variant to use (e.g., asr-10k, captions_asr-10k).",
    )
    args = parser.parse_args()

    if not args.video_path.exists():
        print(f"Error: Video file {args.video_path} not found")
        return

    main(video_path=args.video_path, model=args.model)


if __name__ == "__main__":
    cli_entrypoint()
