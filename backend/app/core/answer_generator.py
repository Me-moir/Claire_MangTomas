import torch
import logging
import time
import threading
import os
import traceback
import re
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

# For GGUF model support
try:
    from llama_cpp import Llama
    LLAMA_CPP_AVAILABLE = True
except ImportError as e:
    LLAMA_CPP_AVAILABLE = False
    logging.warning(f"llama-cpp-python not available: {e}")
    logging.warning("To fix: pip uninstall llama-cpp-python -y && pip install llama-cpp-python==0.2.77")
except OSError as e:
    LLAMA_CPP_AVAILABLE = False
    logging.error(f"DLL load failed: {e}")
    logging.error("CUDA DLLs missing. Using CPU version: pip uninstall llama-cpp-python -y && pip install llama-cpp-python==0.2.77")

# Import settings with fallback
try:
    from app.config import settings
except ImportError:
    # Fallback settings if import fails
    class Settings:
        DEVICE = "cpu"
        CLAIRE_MODEL_PATH = "./models/claire_v1.0.0_q4_k_m.gguf"
        SKIP_MODEL_LOADING = False
        USE_GPU_LAYERS = 0
        ENABLE_GREETING_DETECTION = True
        SHORT_MESSAGE_THRESHOLD = 20
        MODEL_INFERENCE_TIMEOUT = 300
        GENERATION_TIMEOUT_COOLDOWN = 60
        MODEL_CONTEXT_SIZE = 2048
        MODEL_MAX_TOKENS = 1024
        MODEL_TEMPERATURE = 0.3
        MODEL_TOP_P = 0.9
        MODEL_REPEAT_PENALTY = 1.1
        MODEL_BATCH_SIZE = 256
        LLAMA_CPP_THREADS = None
        USE_MMAP = True
        USE_MLOCK = False
        F16_KV_CPU = False
    settings = Settings()

logger = logging.getLogger(__name__)

# Greeting patterns for different languages
GREETING_PATTERNS = {
    'english': {
        'patterns': [
            r'^(hi|hello|hey|greetings?)(?:\s+there)?(?:\s+claire)?[!?.]*$',
            r'^good\s+(morning|afternoon|evening|day)(?:\s+claire)?[!?.]*$',
            r'^(yo|sup|wassup|what\'?s up)(?:\s+claire)?[!?.]*$',
            r'^claire[!?.]*$',
            r'^(thanks?|thank you)(?:\s+claire)?[!?.]*$',
            r'^(bye|goodbye|see you|farewell)(?:\s+claire)?[!?.]*$'
        ],
        'responses': {
            'neutral': [
                "Hello! I'm CLAIRE, your BPI banking assistant. How can I help you today?",
                "Hi there! I'm CLAIRE from BPI. What can I assist you with?",
                "Good day! I'm CLAIRE, here to help with your banking needs."
            ],
            'grateful': [
                "Hello! Thank you for reaching out to BPI. I'm CLAIRE, how may I assist you?",
                "Hi! It's my pleasure to help you. What can I do for you today?"
            ],
            'confused': [
                "Hello! I'm CLAIRE from BPI. I'm here to help clarify any banking questions you have.",
                "Hi there! I'm CLAIRE. Feel free to ask me anything about BPI services."
            ],
            'frustrated': [
                "Hello, I'm CLAIRE. I understand you may need immediate assistance. How can I help?",
                "Hi, I'm CLAIRE from BPI. I'm here to help resolve your concerns."
            ],
            'urgent': [
                "Hello! I'm CLAIRE. I see this may be urgent. How can I assist you right away?",
                "Hi! I'm CLAIRE, ready to help with your urgent banking needs."
            ],
            'worried': [
                "Hello, I'm CLAIRE from BPI. I'm here to help address your concerns.",
                "Hi, I'm CLAIRE. Don't worry, I'm here to assist you with your banking needs."
            ],
            'thanks': [
                "You're welcome! Is there anything else I can help you with?",
                "My pleasure! Feel free to ask if you need more assistance."
            ],
            'bye': [
                "Thank you for choosing BPI! Have a great day!",
                "Goodbye! Feel free to reach out anytime you need assistance."
            ]
        }
    },
    'tagalog': {
        'patterns': [
            r'^(hi|hello|kumusta|musta)(?:\s+claire)?[!?.]*$',
            r'^magandang?\s+(umaga|hapon|gabi|araw)(?:\s+claire)?[!?.]*$',
            r'^claire[!?.]*$',
            r'^(salamat|thank you)(?:\s+claire)?[!?.]*$',
            r'^(bye|paalam|goodbye)(?:\s+claire)?[!?.]*$'
        ],
        'responses': {
            'neutral': [
                "Kumusta! Ako si CLAIRE, ang inyong BPI banking assistant. Paano ko kayo matutulungan?",
                "Hello po! Ako si CLAIRE mula sa BPI. Ano po ang maitutulong ko?"
            ],
            'grateful': [
                "Kumusta po! Salamat sa pagtawag sa BPI. Ako si CLAIRE, paano ko kayo matutulungan?",
                "Hello po! Nagagalak akong makatulong. Ano po ang kailangan ninyo?"
            ],
            'confused': [
                "Kumusta! Ako si CLAIRE mula sa BPI. Nandito ako para linawin ang inyong mga tanong.",
                "Hello po! Ako si CLAIRE. Magtanong lang po kayo tungkol sa BPI services."
            ],
            'frustrated': [
                "Kumusta, ako si CLAIRE. Nauunawaan ko na kailangan ninyo ng tulong. Paano ko kayo matutulungan?",
                "Hello po, ako si CLAIRE mula sa BPI. Nandito ako para resolbahin ang inyong alalahanin."
            ],
            'urgent': [
                "Kumusta! Ako si CLAIRE. Nakikita kong urgent ito. Paano ko kayo matutulungan agad?",
                "Hello po! Ako si CLAIRE, handa na tumulong sa inyong urgent na pangangailangan."
            ],
            'worried': [
                "Kumusta, ako si CLAIRE mula sa BPI. Nandito ako para tulungan kayo sa inyong alalahanin.",
                "Hello po, ako si CLAIRE. Huwag mag-alala, tutulungan ko kayo."
            ],
            'thanks': [
                "Walang anuman po! May iba pa ba akong maitutulong?",
                "Kasiyahan ko pong makatulong! Magtanong lang po kung may kailangan pa."
            ],
            'bye': [
                "Salamat sa pagtitiwala sa BPI! Magandang araw po!",
                "Paalam po! Tumawag lang po ulit kung kailangan ninyo ng tulong."
            ]
        }
    },
    'taglish': {
        'patterns': [
            r'^(hi|hello|kumusta|musta)(?:\s+claire)?[!?.]*$',
            r'^good\s+(morning|afternoon|evening)(?:\s+po)?(?:\s+claire)?[!?.]*$',
            r'^magandang?\s+(umaga|hapon|gabi)(?:\s+claire)?[!?.]*$',
            r'^claire[!?.]*$',
            r'^(thanks?|salamat|thank you)(?:\s+claire)?[!?.]*$',
            r'^(bye|paalam|goodbye|see you)(?:\s+claire)?[!?.]*$'
        ],
        'responses': {
            'neutral': [
                "Hello po! I'm CLAIRE, your BPI banking assistant. Paano ko kayo matutulungan today?",
                "Hi! Ako si CLAIRE from BPI. What can I help you with po?"
            ],
            'grateful': [
                "Hello po! Thank you sa pagtawag sa BPI. I'm CLAIRE, how can I assist you?",
                "Hi! It's my pleasure to help you po. Ano ang need niyo today?"
            ],
            'confused': [
                "Hello! I'm CLAIRE from BPI. I'm here para i-clarify any questions ninyo.",
                "Hi po! Ako si CLAIRE. Feel free to ask about BPI services."
            ],
            'frustrated': [
                "Hello, I'm CLAIRE. I understand na you need immediate help. Paano ko kayo matutulungan?",
                "Hi po, ako si CLAIRE from BPI. I'm here para i-resolve ang concerns ninyo."
            ],
            'urgent': [
                "Hello! I'm CLAIRE. I see this is urgent po. How can I help you right away?",
                "Hi! Ako si CLAIRE, ready to help with your urgent needs po."
            ],
            'worried': [
                "Hello, I'm CLAIRE from BPI. I'm here para i-address ang worries ninyo.",
                "Hi po, ako si CLAIRE. Don't worry, I'll help you with your banking needs."
            ],
            'thanks': [
                "You're welcome po! May iba pa ba akong maitutulong?",
                "My pleasure po! Just ask if you need more help."
            ],
            'bye': [
                "Thank you for choosing BPI! Have a great day po!",
                "Goodbye po! Feel free to message anytime you need help."
            ]
        }
    }
}

class AnswerGenerator:
    def __init__(self):
        """Initialize with extensive error handling"""
        try:
            self.device = torch.device(settings.DEVICE)
            self.model = None
            self.generation_lock = threading.Lock()
            self.is_generating = False
            self.last_timeout = 0
            self.timeout_cooldown = settings.GENERATION_TIMEOUT_COOLDOWN
            self._stop_generation = False
            
            # Model path for GGUF (auto-selected based on device)
            self.model_path = settings.CLAIRE_MODEL_PATH
            
            # Set timeout based on device
            self.generation_timeout = settings.MODEL_INFERENCE_TIMEOUT
            
            # GPU layers configuration
            self.n_gpu_layers = settings.USE_GPU_LAYERS
            
            logger.info(f"Using {settings.DEVICE} mode with {self.generation_timeout}s timeout")
            logger.info(f"Model: {os.path.basename(self.model_path)}")
            if self.n_gpu_layers > 0:
                logger.info(f"GPU layers to offload: {self.n_gpu_layers}")
            
            # Check if we should skip model loading
            if getattr(settings, 'SKIP_MODEL_LOADING', False):
                logger.warning("Skipping model loading (SKIP_MODEL_LOADING=True)")
                self.model = None
            elif not LLAMA_CPP_AVAILABLE:
                logger.error("llama-cpp-python is not installed. Cannot load GGUF model.")
                self.model = None
            else:
                # Try to load model but don't fail if it doesn't work
                try:
                    self._load_model()
                except Exception as e:
                    logger.error(f"Failed to load model during init: {e}")
                    logger.error(f"Traceback: {traceback.format_exc()}")
                    self.model = None
                
        except Exception as e:
            logger.error(f"Critical error in AnswerGenerator init: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            # Set minimal defaults to prevent crashes
            self.model = None
            self.generation_lock = threading.Lock()
            self.is_generating = False
            self.last_timeout = 0
            self.timeout_cooldown = 60
            self._stop_generation = False
            self.generation_timeout = 300
            self.device = torch.device("cpu")
    
    def _is_greeting_message(self, text: str, language: str) -> tuple[bool, str, str]:
        """
        Check if message is a greeting and return appropriate response
        Returns: (is_greeting, greeting_type, response)
        """
        if not settings.ENABLE_GREETING_DETECTION:
            return False, None, None
        
        # Clean and normalize text
        text = text.strip().lower()
        
        # Check if message is too short to be a real query
        if len(text) > settings.SHORT_MESSAGE_THRESHOLD:
            return False, None, None
        
        # Get patterns for the detected language
        lang_patterns = GREETING_PATTERNS.get(language, GREETING_PATTERNS['english'])
        
        # Check each pattern
        for pattern in lang_patterns['patterns']:
            if re.match(pattern, text, re.IGNORECASE):
                # Determine greeting type
                if re.search(r'(thanks?|thank you|salamat)', text, re.IGNORECASE):
                    return True, 'thanks', None
                elif re.search(r'(bye|goodbye|paalam|farewell|see you)', text, re.IGNORECASE):
                    return True, 'bye', None
                else:
                    return True, 'greeting', None
        
        return False, None, None
    
    def _get_greeting_response(self, greeting_type: str, language: str, emotion: str) -> str:
        """Get appropriate greeting response based on type, language, and emotion"""
        import random
        
        lang_responses = GREETING_PATTERNS.get(language, GREETING_PATTERNS['english'])
        
        # Map greeting type to response category
        if greeting_type == 'thanks':
            responses = lang_responses['responses'].get('thanks', [])
        elif greeting_type == 'bye':
            responses = lang_responses['responses'].get('bye', [])
        else:
            # Get emotion-specific responses or default to neutral
            responses = lang_responses['responses'].get(emotion, 
                                                       lang_responses['responses']['neutral'])
        
        if responses:
            return random.choice(responses)
        
        # Fallback response
        return "Hello! I'm CLAIRE, your BPI banking assistant. How can I help you today?"
        
    def _load_model(self):
        """Load GGUF quantized model"""
        try:
            logger.info(f"Loading GGUF model from {self.model_path}...")
            
            # Check if model file exists
            if not os.path.exists(self.model_path):
                logger.error(f"Model file does not exist: {self.model_path}")
                raise FileNotFoundError(f"Model file not found: {self.model_path}")
            
            # Check file size
            file_size = os.path.getsize(self.model_path) / (1024 * 1024 * 1024)  # Convert to GB
            logger.info(f"Model file size: {file_size:.2f} GB")
            
            # Configure based on device
            if settings.DEVICE == "cuda" and torch.cuda.is_available():
                # GPU configuration
                self.model = Llama(
                    model_path=self.model_path,
                    n_ctx=settings.MODEL_CONTEXT_SIZE,
                    n_batch=settings.MODEL_BATCH_SIZE,
                    n_gpu_layers=self.n_gpu_layers,
                    n_threads=8,  # CPU threads for non-offloaded operations
                    use_mmap=settings.USE_MMAP,
                    use_mlock=settings.USE_MLOCK,
                    verbose=False
                )
                logger.info(f"GPU model loaded with {self.n_gpu_layers} layers offloaded")
            else:
                # CPU configuration - optimized
                n_threads = settings.LLAMA_CPP_THREADS
                
                self.model = Llama(
                    model_path=self.model_path,
                    n_ctx=settings.MODEL_CONTEXT_SIZE,
                    n_batch=settings.MODEL_BATCH_SIZE,
                    n_gpu_layers=0,
                    n_threads=n_threads,
                    use_mmap=settings.USE_MMAP,
                    use_mlock=settings.USE_MLOCK,
                    seed=-1,
                    f16_kv=settings.F16_KV_CPU,
                    logits_all=False,
                    vocab_only=False,
                    embedding=False,
                    verbose=False
                )
                logger.info(f"CPU model loaded with {n_threads} threads")
            
            logger.info(f"GGUF model loaded successfully")
            
        except FileNotFoundError as e:
            logger.error(f"File not found: {e}")
            self.model = None
            
        except ImportError as e:
            logger.error(f"Import error (missing dependency?): {e}")
            logger.error("Install llama-cpp-python: pip install llama-cpp-python")
            self.model = None
            
        except Exception as e:
            logger.error(f"Failed to load GGUF model: {e}")
            logger.error(f"Full traceback: {traceback.format_exc()}")
            logger.warning("GGUF model not available - will use retrieval-only for all responses")
            self.model = None
    
    def _should_skip_generation(self) -> bool:
        """Check if we should skip generation due to recent timeout"""
        try:
            current_time = time.time()
            if current_time - self.last_timeout < self.timeout_cooldown:
                remaining_cooldown = self.timeout_cooldown - (current_time - self.last_timeout)
                logger.info(f"Skipping generation due to recent timeout (cooldown: {remaining_cooldown:.0f}s remaining)")
                return True
            return False
        except Exception as e:
            logger.error(f"Error in _should_skip_generation: {e}")
            return True  # Skip on error
    
    def generate_answer(
        self,
        question: str,
        language: str,
        emotion: str,
        contexts: List[Dict[str, Any]],
        extracted_text: str = None
    ) -> Dict[str, Any]:
        """
        Generate answer using CLAIRE GGUF model with retrieved contexts.
        If timeout or error, return formatted retrieved contexts directly.
        """
        
        # Initialize result
        result = {
            'answer': '',
            'success': False,
            'method': 'none',
            'generation_time': 0,
            'timeout': False
        }
        
        try:
            start_time = time.time()
            
            # Validate inputs
            if not question:
                question = ""
            if not language:
                language = "english"
            if not emotion:
                emotion = "neutral"
            if contexts is None:
                contexts = []
            
            # Check if this is a greeting message
            is_greeting, greeting_type, _ = self._is_greeting_message(question, language)
            if is_greeting:
                logger.info(f"Detected greeting message: {greeting_type}")
                result['answer'] = self._get_greeting_response(greeting_type, language, emotion)
                result['success'] = True
                result['method'] = 'greeting_response'
                result['generation_time'] = time.time() - start_time
                return result
                
            # Check if we have contexts
            if not contexts or len(contexts) == 0:
                result['answer'] = self._get_no_context_response(language, emotion)
                result['method'] = 'no_context'
                result['success'] = True
                return result
            
            # Skip generation if model not loaded or in cooldown
            if self.model is None:
                logger.info("Model not loaded, using retrieval-only")
                result['answer'] = self._format_retrieved_contexts(
                    question, language, emotion, contexts, extracted_text
                )
                result['success'] = True
                result['method'] = 'retrieval_only_no_model'
                return result
            
            # Check if we're in cooldown period after recent timeout
            if self._should_skip_generation():
                logger.info("Using retrieval-only due to recent timeout cooldown")
                result['answer'] = self._format_retrieved_contexts(
                    question, language, emotion, contexts, extracted_text
                )
                result['success'] = True
                result['method'] = 'retrieval_only_cooldown'
                return result
            
            # Check if we're already generating
            with self.generation_lock:
                if self.is_generating:
                    logger.warning("Already generating, skipping to retrieval-only")
                    result['answer'] = self._format_retrieved_contexts(
                        question, language, emotion, contexts, extracted_text
                    )
                    result['success'] = True
                    result['method'] = 'retrieval_only_busy'
                    return result
                
                self.is_generating = True
                self._stop_generation = False
            
            try:
                # Try CLAIRE generation
                logger.info(f"Attempting CLAIRE GGUF generation...")
                
                try:
                    # Direct generation with timeout wrapper
                    generated_answer = self._generate_with_timeout_wrapper(
                        question, language, emotion, contexts, extracted_text
                    )
                    
                    if generated_answer:
                        result['answer'] = generated_answer
                        result['success'] = True
                        result['method'] = 'claire_rag'
                        result['generation_time'] = time.time() - start_time
                        logger.info(f"✓ CLAIRE RAG generation successful in {result['generation_time']:.2f}s")
                        return result
                    else:
                        logger.warning("Generation returned empty result")
                        
                except TimeoutError:
                    logger.warning("Generation timed out")
                    result['timeout'] = True
                    self.last_timeout = time.time()
                    
                except Exception as e:
                    logger.error(f"Error during generation: {e}")
                    logger.error(f"Traceback: {traceback.format_exc()}")
                
                # FALLBACK: Return formatted retrieved contexts
                logger.info("Using retrieval-only response...")
                
                formatted_answer = self._format_retrieved_contexts(
                    question, language, emotion, contexts, extracted_text
                )
                
                result['answer'] = formatted_answer
                result['success'] = True
                result['method'] = 'retrieval_only'
                result['generation_time'] = time.time() - start_time
                
                # Add timeout notification if applicable
                if result.get('timeout', False):
                    timeout_note = self._get_timeout_note(language)
                    result['answer'] = f"{formatted_answer}\n\n{timeout_note}"
                    
            finally:
                with self.generation_lock:
                    self.is_generating = False
                    self._stop_generation = False
                    
        except Exception as e:
            logger.error(f"Critical error in generate_answer: {e}")
            logger.error(f"Full traceback: {traceback.format_exc()}")
            result['answer'] = self._get_error_response(language if 'language' in locals() else 'english')
            result['method'] = 'error'
            result['success'] = False
            
        return result
    
    def _generate_with_timeout_wrapper(
        self,
        question: str,
        language: str,
        emotion: str,
        contexts: List[Dict[str, Any]],
        extracted_text: str
    ) -> Optional[str]:
        """
        Wrapper for generation with timeout.
        Returns generated text or None if timeout/error.
        """
        
        timeout_seconds = self.generation_timeout
        
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                self._generate_with_claire_gguf_safe,
                question, language, emotion, contexts, extracted_text
            )
            
            try:
                result = future.result(timeout=timeout_seconds)
                return result
                
            except FutureTimeoutError:
                logger.warning(f"Generation timed out after {timeout_seconds}s")
                self._stop_generation = True
                future.cancel()
                
                # Clean up
                if hasattr(self, 'device') and self.device.type == "cuda":
                    try:
                        torch.cuda.empty_cache()
                    except:
                        pass
                
                raise TimeoutError("Generation timed out")
                
            except Exception as e:
                logger.error(f"Error in generation wrapper: {e}")
                return None
    
    def _generate_with_claire_gguf_safe(
        self,
        question: str,
        language: str,
        emotion: str,
        contexts: List[Dict[str, Any]],
        extracted_text: str
    ) -> Optional[str]:
        """
        Safe generation with GGUF model using llama-cpp-python.
        Uses Alpaca format matching the training template.
        """
        
        try:
            # Validate model
            if self.model is None:
                logger.error("Model is None")
                return None
            
            # Format contexts exactly as in training (4 contexts with scores)
            context_texts = []
            for i, ctx in enumerate(contexts[:4]):  # Use up to 4 contexts as in training
                try:
                    score = ctx.get('score', 0)
                    content = ctx.get('content', '')[:500]
                    context_texts.append(f"Context {i+1} (Score: {score:.2f}): {content}")
                except Exception as e:
                    logger.warning(f"Error formatting context {i}: {e}")
                    continue
            
            # Ensure we have exactly 4 contexts (pad with empty if needed)
            while len(context_texts) < 4:
                context_texts.append(f"Context {len(context_texts)+1} (Score: 0.00): No additional context available.")
            
            formatted_contexts = "\n\n".join(context_texts)
            
            # Add extracted text if available (prepend to contexts)
            if extracted_text:
                formatted_contexts = f"User Document: {extracted_text[:500]}\n\n{formatted_contexts}"
            
            # Create prompt using EXACT Alpaca format from training
            prompt = (
                f"### Instruction:\n"
                f"You are CLAIRE (Conversational Language AI for Resolution & Engagement), "
                f"a banking customer assistant working for BPI (Bank of the Philippine Islands). "
                f"Your role is to answer customer questions accurately, clearly, and empathetically. "
                f"Given the question, its identified language and emotion, and four context documents, "
                f"generate a response that is linguistically accurate, emotionally appropriate, "
                f"and grounded in the most relevant context.\n\n"
                f"### Input:\n"
                f"Question: {question}\n"
                f"Language: {language}\n"
                f"Emotion: {emotion}\n\n"
                f"Contexts:\n{formatted_contexts}\n\n"
                f"### Output:\n"
            )
            
            # Generate response using llama-cpp-python
            logger.debug(f"Generating with Alpaca format prompt ({len(prompt)} chars)")
            
            response = self.model(
                prompt,
                max_tokens=settings.MODEL_MAX_TOKENS,
                temperature=settings.MODEL_TEMPERATURE,
                top_p=settings.MODEL_TOP_P,
                echo=False,
                stop=["### Instruction:", "### Input:", "### Output:", "\n\n### ", "</s>"],
                repeat_penalty=settings.MODEL_REPEAT_PENALTY,
            )
            
            # Extract the generated text
            if response and 'choices' in response and len(response['choices']) > 0:
                generated_text = response['choices'][0]['text'].strip()
                
                # Clean up the response (remove any Alpaca artifacts)
                generated_text = self._clean_generated_text(generated_text)
                
                return generated_text if generated_text else None
            else:
                logger.warning("No valid response from model")
                return None
            
        except Exception as e:
            logger.error(f"Error in safe GGUF generation: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return None
    
    def _clean_generated_text(self, text: str) -> str:
        """Clean up generated text by removing artifacts from Alpaca format"""
        try:
            # Remove any Alpaca format artifacts that might appear
            text = text.replace("### Instruction:", "")
            text = text.replace("### Input:", "")
            text = text.replace("### Output:", "")
            text = text.replace("###", "")
            
            # Remove any special tokens
            text = text.replace("</s>", "").replace("<s>", "")
            text = text.replace("<|endoftext|>", "")
            
            # Remove any context references that might leak through
            lines = []
            for line in text.split('\n'):
                # Skip lines that look like context headers
                if not line.strip().startswith("Context ") or not "(Score:" in line:
                    lines.append(line.strip())
            
            text = '\n'.join([line for line in lines if line])
            
            return text
        except Exception as e:
            logger.error(f"Error cleaning generated text: {e}")
            return text
    
    def _format_retrieved_contexts(
        self,
        question: str,
        language: str,
        emotion: str,
        contexts: List[Dict[str, Any]],
        extracted_text: str = None
    ) -> str:
        """Format retrieved contexts as direct answer"""
        
        try:
            best_context = ""
            
            if contexts and len(contexts) > 0:
                best_context = contexts[0].get('content', '')
                
                if len(contexts) > 1 and contexts[1].get('score', 0) > 0.8:
                    second_context = contexts[1].get('content', '')[:300]
                    best_context = f"{best_context}\n\n{second_context}"
            
            if extracted_text:
                combined_info = f"From your document:\n{extracted_text[:300]}\n\nFrom our knowledge base:\n{best_context[:400]}"
            else:
                combined_info = best_context[:700] if best_context else "No information available."
            
            if language == 'tagalog':
                response = f"Batay sa aming impormasyon:\n\n{combined_info}"
            elif language == 'taglish':
                response = f"Based sa our information:\n\n{combined_info}"
            else:
                response = f"Based on our information:\n\n{combined_info}"
            
            response = self._add_emotion_response(response, language, emotion)
            return response
            
        except Exception as e:
            logger.error(f"Error formatting contexts: {e}")
            return "I apologize, but I encountered an error processing your request. Please try again."
    
    def _add_emotion_response(self, response: str, language: str, emotion: str) -> str:
        """Add emotion-appropriate ending"""
        
        try:
            if emotion in ['frustrated', 'urgent', 'worried']:
                if language == 'tagalog':
                    response += "\n\n🆘 Nauunawaan namin ang inyong sitwasyon. Para sa agarang tulong, tumawag sa 889-10000."
                elif language == 'taglish':
                    response += "\n\n🆘 We understand your situation. For immediate help, please call 889-10000."
                else:
                    response += "\n\n🆘 We understand your concern. For immediate assistance, please call 889-10000."
                    
            elif emotion == 'grateful':
                if language == 'tagalog':
                    response += "\n\n😊 Salamat sa inyong tiwala sa BPI!"
                elif language == 'taglish':
                    response += "\n\n😊 Thank you for trusting BPI!"
                else:
                    response += "\n\n😊 Thank you for choosing BPI!"
                    
            elif emotion == 'confused':
                if language == 'tagalog':
                    response += "\n\n💡 Kung may iba pang tanong, huwag mag-atubiling magtanong."
                elif language == 'taglish':
                    response += "\n\n💡 If you have more questions, feel free to ask."
                else:
                    response += "\n\n💡 If you need further clarification, please don't hesitate to ask."
                    
        except Exception as e:
            logger.error(f"Error adding emotion response: {e}")
            
        return response
    
    def _get_timeout_note(self, language: str) -> str:
        """Get timeout notification"""
        try:
            if language == 'tagalog':
                return "⏱️ Paalala: Direktang galing sa knowledge base ang sagot dahil nag-timeout ang AI generation."
            elif language == 'taglish':
                return "⏱️ Note: This is directly from our knowledge base since AI generation timed out."
            else:
                return "⏱️ Note: This response is directly from our knowledge base as AI generation exceeded time limit."
        except:
            return "⏱️ Note: Response from knowledge base (generation timeout)."
    
    def _get_no_context_response(self, language: str, emotion: str) -> str:
        """Response when no relevant context is found"""
        try:
            if language == 'tagalog':
                response = "Pasensya na, wala akong nakitang tugmang impormasyon para sa inyong tanong."
            elif language == 'taglish':
                response = "Sorry, I couldn't find matching information for your question."
            else:
                response = "I apologize, but I couldn't find relevant information for your question."
            
            response += "\n\n📞 Please contact our customer service at 889-10000 for personalized assistance."
            return response
        except:
            return "I couldn't find relevant information. Please call 889-10000 for assistance."
    
    def _get_error_response(self, language: str) -> str:
        """Error response"""
        try:
            if language == 'tagalog':
                return "⚠️ Pasensya, may technical error. Tumawag sa 889-10000 para sa tulong."
            else:
                return "⚠️ Sorry, a technical error occurred. Please call 889-10000 for assistance."
        except:
            return "⚠️ Technical error. Please call 889-10000 for assistance."
    
    def shutdown(self):
        """Clean shutdown"""
        try:
            logger.info("Shutting down AnswerGenerator...")
            
            # Set stop flag
            self._stop_generation = True
            
            # Clear model from memory
            if self.model is not None:
                del self.model
                self.model = None
            
            # Clean up CUDA memory if applicable
            if hasattr(self, 'device') and self.device.type == "cuda":
                try:
                    torch.cuda.empty_cache()
                except:
                    pass
            
            logger.info("AnswerGenerator shutdown complete")
            
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")