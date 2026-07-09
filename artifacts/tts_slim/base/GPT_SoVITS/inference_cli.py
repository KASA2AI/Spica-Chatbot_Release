import argparse
import os
import soundfile as sf
import os
# 删除错误代理
for k in ["all_proxy", "ALL_PROXY", "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"]:
    os.environ.pop(k, None)
from tools.i18n.i18n import I18nAuto
from GPT_SoVITS.inference_webui import change_gpt_weights, change_sovits_weights, get_tts_wav

i18n = I18nAuto()


def synthesize(
    GPT_model_path,
    SoVITS_model_path,
    ref_audio_path,
    ref_text_path,
    ref_language,
    target_text_path,
    target_language,
    output_path,
    inp_refs_path=None,  # 新增参数
    how_to_cut=i18n("凑四句一切"),
):
    # Read reference text    with open(ref_text_path, "r", encoding="utf-8") as file:
    ref_text = ref_text_path

    # Read target text

    target_text = target_text_path

    # Change model weights
    change_gpt_weights(gpt_path=GPT_model_path)
    for _ in change_sovits_weights(
        sovits_path=SoVITS_model_path,
        prompt_language=i18n(ref_language),  # 传入参考语言
        text_language=i18n(target_language)  # 传入目标语言
    ):
        pass
    # Synthesize audio
    synthesis_result = get_tts_wav(
        ref_wav_path=ref_audio_path,
        prompt_text=ref_text,
        prompt_language=i18n(ref_language),
        text=target_text,
        text_language=i18n(target_language),
        top_p=1,
        temperature=1,
        inp_refs = inp_refs_path,
        how_to_cut = how_to_cut,
        pause_second = 0.3,
        speed=1,
        top_k=15,
        ref_free=False,

    )

    result_list = list(synthesis_result)

    if result_list:
        last_sampling_rate, last_audio_data = result_list[-1]
        output_wav_path = os.path.join(output_path, "output.wav")
        sf.write(output_wav_path, last_audio_data, last_sampling_rate)
        print(f"Audio saved to {output_wav_path}")


def main():
    # parser = argparse.ArgumentParser(description="GPT-SoVITS Command Line Tool")
    # parser.add_argument("--gpt_model", required=True, help="Path to the GPT model file")
    # parser.add_argument("--sovits_model", required=True, help="Path to the SoVITS model file")
    # parser.add_argument("--ref_audio", required=True, help="Path to the reference audio file")
    # parser.add_argument("--ref_text", required=True, help="Path to the reference text file")
    # parser.add_argument(
    #     "--ref_language", required=True, choices=["中文", "英文", "日文"], help="Language of the reference audio"
    # )
    # parser.add_argument("--target_text", required=True, help="Path to the target text file")
    # parser.add_argument(
    #     "--target_language",
    #     required=True,
    #     choices=["中文", "英文", "日文", "中英混合", "日英混合", "多语种混合"],
    #     help="Language of the target text",
    # )
    # parser.add_argument("--output_path", required=True, help="Path to the output directory")
    # parser.add_argument("--inp_refs", help="Path to folder containing reference audio files")  # 新增参数
    # parser.add_argument("--how_to_cut", help="how to cut")  # 新增参数
    #
    # args = parser.parse_args()

    GPT_MODEL = r"/home/san/ai_code/Spica-Chatbot/GPT-SoVITS-v2pro-20250604-nvidia50/GPT_weights_v2ProPlus/spcia-e25.ckpt"
    SOVITS_MODEL = r"/home/san/ai_code/Spica-Chatbot/GPT-SoVITS-v2pro-20250604-nvidia50/SoVITS_weights_v2ProPlus/spcia_e12_s1932.pth"
    REF_AUDIO = r"/home/san/ai_code/Spica-Chatbot/spica_data/egg1/あそこは風の一族が封じた厄災の力が残ってるわ.wav"
    REF_TEXT = "あそこは風の一族が封じた厄災の力が残ってるわ"
    REF_LANGUAGE = "日文"
    TARGET_TEXT = "本メールを受信されたメールアドレスと、ログイン時のアカウントに登録されているメールアドレスが別のものですとクーポンが確認できない場合がございます。複数のアカウントをお持ちでないかご確認ください。"
    TARGET_LANGUAGE = "日文"
    OUTPUT_PATH = r"/home/san/ai_code/Spica-Chatbot/output_wav_test"
    inp_refs = r"/home/san/ai_code/Spica-Chatbot/spica_data/egg1/refs"
    # synthesize(
    #     args.gpt_model,
    #     args.sovits_model,
    #     args.ref_audio,
    #     args.ref_text,
    #     args.ref_language,
    #     args.target_text,
    #     args.target_language,
    #     args.output_path,
    #     args.inp_refs,
    #     args.how_to_cut,
    # )
    synthesize(
        GPT_MODEL,
        SOVITS_MODEL,
        REF_AUDIO,
        REF_TEXT,
        REF_LANGUAGE,
        TARGET_TEXT,
        TARGET_LANGUAGE,
        OUTPUT_PATH,
        inp_refs,
    )


if __name__ == "__main__":
    main()
